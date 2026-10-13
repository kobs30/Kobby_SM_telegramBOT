import argparse
import json
import os
from pathlib import Path
import secrets

from bridge.api import API, APIError, Publisher, origin
from bridge.storage import Store


def load_config(path):
    config = json.loads(Path(path).read_text())
    for key in ('kobby_base', 'watcher_base', 'public_base'):
        config[key] = origin(config[key])
    for key, default in (('media_cache_bytes', 1024 ** 3), ('media_retention_days', 3)):
        if type(config.get(key, default)) is not int or config.get(key, default) <= 0:
            raise ValueError(key + ' must be a positive integer')
    return config


def provision(store, api, username):
    account = store.secret('publisher')
    if account and account.get('token'):
        me = Publisher(store, api).request('GET', '/api/v1/me')
        if me != {'id': account['username'], 'kind': 'user'}:
            raise ValueError('Publisher identity mismatch')
        return store.secret('publisher')
    for _ in range(10):
        if not account:
            account = {'username': username, 'password': secrets.token_urlsafe(36)}
            # Save credentials before registration: a timeout must be recoverable.
            store.put_secret('publisher', account)
        try:
            result = api.request('POST', '/api/v1/auth/login', account)
        except APIError as error:
            if error.status != 401:
                raise
            try:
                result = api.request('POST', '/api/v1/auth/register', account)
            except APIError as registration_error:
                if registration_error.status != 409 or registration_error.code != 'username_taken':
                    raise
                # Only a definitive conflict permits choosing a new username.
                # Recover our own earlier registration before abandoning the name.
                try:
                    result = api.request('POST', '/api/v1/auth/login', account)
                except APIError as login_error:
                    if login_error.status != 401:
                        raise
                    username = username[:22] + '_' + str(secrets.randbelow(90000000) + 10000000)
                    account = None
                    continue
        if result.get('user_id') != account['username']:
            raise ValueError('Registration identity mismatch')
        account.update(token=result['token'], expires_at=result['expires_at'])
        store.put_secret('publisher', account)
        return account
    raise ValueError('Could not allocate a publisher username')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['provision', 'status', 'set-watcher-key', 'retry'])
    parser.add_argument('--username', default='tlg_bridge')
    args = parser.parse_args()
    config = load_config(args.config)
    store = Store(config['data_dir'], create_key=args.command == 'provision', key_path=config['key_file'])
    try:
        store.bind('kobby_base', config['kobby_base'])
        store.bind('watcher_base', config['watcher_base'])
        if args.command == 'provision':
            account = provision(store, API(config['kobby_base']), args.username)
            print(json.dumps({'user_id': account['username'], 'expires_at': account['expires_at'], 'credentials': 'encrypted'}))
        elif args.command == 'set-watcher-key':
            import sys
            key = sys.stdin.read().strip()
            if len(key) < 32:
                raise ValueError('Watcher API key is too short')
            store.put_secret('watcher_key', key)
            print('Watcher key stored encrypted')
        elif args.command == 'retry':
            store.execute("UPDATE connections SET state='pending',next_at=0,attempts=0 WHERE state!='joined'")
            store.execute("UPDATE outbox SET next_at=0,attempts=0 WHERE state='pending'")
            store.execute("UPDATE preparations SET next_at=0,attempts=0 WHERE state='pending'")
            print('Pending tasks scheduled; unavailable mirrors require inspection')
        else:
            print(json.dumps({'sources': store.rows('SELECT id,username,channel_id,state,error FROM sources'),
                              'connections': store.rows('SELECT owner_id,username,state,error FROM connections'),
                              'queue': store.rows('SELECT state,COUNT(*) AS count FROM outbox GROUP BY state'),
                              'photos': store.rows('SELECT state,error,COUNT(*) AS count FROM preparations GROUP BY state,error')}, ensure_ascii=False))
    except APIError as error:
        parser.exit(1, f'{error}\n')
    finally:
        store.close()


if __name__ == '__main__':
    main()
