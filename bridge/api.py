import time
from urllib.parse import urlsplit

import httpx


class APIError(Exception):
    def __init__(self, status, code, retry_after=0):
        self.status, self.code, self.retry_after = status, code, retry_after
        super().__init__(f'HTTP {status}: {code}')


def origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')):
        raise ValueError('Expected a trusted HTTPS origin on port 443')
    return value.rstrip('/')


class API:
    def __init__(self, base, header='Authorization'):
        self.base, self.header = origin(base), header
        self.client = httpx.Client(follow_redirects=False, trust_env=False, timeout=5)

    def request(self, method, path, body=None, token=None):
        headers = {}
        if token:
            headers[self.header] = 'Bearer ' + token if self.header == 'Authorization' else token
        try:
            with self.client.stream(method, self.base + path, json=body, headers=headers) as response:
                chunks, length = [], 0
                for chunk in response.iter_bytes():
                    length += len(chunk)
                    if length > 4 * 1024 * 1024:
                        raise APIError(502, 'response_too_large')
                    chunks.append(chunk)
                import json
                try:
                    result = json.loads(b''.join(chunks))
                except ValueError:
                    result = {}
                if response.status_code not in (200, 201, 204):
                    code = result.get('error') if isinstance(result, dict) else None
                    # Do not propagate arbitrary remote text into logs or status pages.
                    if not isinstance(code, str) or not code.replace('_', '').isalnum():
                        code = 'remote_error'
                    try:
                        retry = max(0, int(response.headers.get('Retry-After', '0')))
                    except ValueError:
                        retry = 60
                    raise APIError(response.status_code, code[:80], retry)
                return result
        except httpx.HTTPError:
            raise APIError(503, 'network_error') from None


class Publisher:
    def __init__(self, store, api):
        self.store, self.api = store, api

    def request(self, method, path, body=None):
        account = self.store.secret('publisher')
        if not account or not account.get('token'):
            raise APIError(401, 'publisher_setup_required')
        if account.get('expires_at', 0) <= time.time() + 300:
            account = self.login(account)
        try:
            return self.api.request(method, path, body, account['token'])
        except APIError as error:
            if error.status != 401:
                raise
            account = self.login(account)
            return self.api.request(method, path, body, account['token'])

    def login(self, account):
        result = self.api.request('POST', '/api/v1/auth/login',
                                  {'username': account['username'], 'password': account['password']})
        if result.get('user_id') != account['username']:
            raise APIError(403, 'publisher_identity_mismatch')
        account.update(token=result['token'], expires_at=result['expires_at'])
        self.store.put_secret('publisher', account)
        return account
