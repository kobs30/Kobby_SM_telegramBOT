import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from bridge.api import APIError
from bridge.cli import provision
from bridge.render import render
from bridge.service import Bridge, signature
from bridge.storage import Store
from bridge.web import create_app


class Kobby:
    def __init__(self):
        self.credentials = {}
        self.claims = {}
        self.channels = {}
        self.sent = {}
        self.joins = set()
        self.requests = []
        self.lose_create = self.lose_send = self.expire_user = self.login_denied = False

    def request(self, method, path, body=None, token=None):
        self.requests.append((method, path, copy.deepcopy(body), token))
        if path == '/api/v1/bot/connect':
            eid = body['event_id']
            if eid in self.claims and self.claims[eid] != body['exchange_id']:
                raise APIError(401, 'unauthorized')
            self.claims[eid] = body['exchange_id']
            return copy.deepcopy(self.credentials[eid])
        if path == '/api/v1/bot/me':
            credential = next((c for c in self.credentials.values() if c['api_token'] == token), None)
            if not credential:
                raise APIError(401, 'unauthorized')
            return {k: copy.deepcopy(credential[k]) for k in ('bot_id','owner_id','permissions','all_personal_chats','chat_ids')}
        if path == '/api/v1/auth/login':
            if self.login_denied:
                raise APIError(401, 'invalid_credentials')
            self.expire_user = False
            return {'user_id': 'tlg_bridge', 'token': 'renewed-user-token', 'expires_at': time.time()+3600}
        if path.startswith('/api/v1/bot/communities/join'):
            c = next(c for c in self.credentials.values() if c['api_token'] == token)
            if 'communities.join' not in c['permissions']:
                raise APIError(403, 'bot_permission_required')
            self.joins.add((c['owner_id'], body['id']))
            return {'id': body['id'], 'viewer_role': 'member'}
        if self.expire_user:
            raise APIError(401, 'unauthorized')
        if path == '/api/v1/communities/create':
            old = self.channels.setdefault(body['id'], copy.deepcopy(body))
            if old != body:
                raise APIError(409, 'chat_id_conflict')
            if self.lose_create:
                self.lose_create = False
                raise APIError(503, 'network_error')
            return copy.deepcopy(old)
        if path.startswith('/api/v1/communities/info?id='):
            cid = path.split('=')[1]
            if cid not in self.channels:
                raise APIError(403, 'chat_not_allowed')
            return {'chat': dict(self.channels[cid], visibility='public', viewer_role='owner')}
        if path == '/api/v1/messages':
            if body['chat_id'] not in self.channels:
                raise APIError(403, 'chat_not_allowed')
            key = body['client_message_id']
            duplicate = key in self.sent
            old = self.sent.setdefault(key, copy.deepcopy(body))
            if old != body:
                raise APIError(409, 'idempotency_conflict')
            if self.lose_send:
                self.lose_send = False
                raise APIError(503, 'network_error')
            return {'message': old, 'duplicate': duplicate}
        raise AssertionError(path)


class Watcher:
    def __init__(self):
        self.channel = {'id': 100, 'username': 'alexavni', 'title': 'Test channel', 'last_seen': 10, 'status': 'active'}
        self.messages = [{'id': 10, 'text': 'Historical post'}]
        self.hooks = {}
        self.failure = None

    def request(self, method, path, body=None, token=None):
        if self.failure:
            raise self.failure
        assert token == 'watcher-key'
        if path == '/v1/channels':
            return copy.deepcopy(self.channel)
        if '/messages' in path:
            return {'items': copy.deepcopy(self.messages), 'retention_limit': 5}
        if path.endswith('/webhooks'):
            return self.hooks.setdefault(body['url'], {'id': 'hook-1', 'secret': 'w'*40})
        raise AssertionError(path)


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name, create_key=True)
        self.store.put_secret('publisher', {'username': 'tlg_bridge', 'password': 'test-only-generated-password',
                                           'token': 'user-token', 'expires_at': time.time()+3600})
        self.store.put_secret('watcher_key', 'watcher-key')
        self.kobby, self.watcher = Kobby(), Watcher()
        self.config = {'kobby_base': 'https://kobby.example', 'watcher_base': 'https://watcher.example',
                       'public_base': 'https://bridge.example', 'poll_interval': 60}
        self.bridge = Bridge(self.config, self.store, self.kobby, self.watcher)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def event(self, number=1, owner='alice', bot_number=None, *, permissions=None,
              chat_ids=None, all_personal_chats=False):
        eid, bid = f'{number:064x}', f'{bot_number or number:032x}'
        credential = {'event_id': eid, 'bot_id': bid, 'owner_id': owner,
                      'permissions': ['communities.join'] if permissions is None else permissions,
                      'chat_ids': [] if chat_ids is None else chat_ids, 'all_personal_chats': all_personal_chats,
                      'api_token': f'bot-token-{number}', 'webhook_secret': 's'*40}
        self.kobby.credentials[eid] = credential
        event = {k:v for k,v in credential.items() if k not in ('api_token','webhook_secret')}
        event.update(version=1, event='bot.connected', connection_code='c'*64, expires_at=int(time.time())+600)
        raw = json.dumps(event).encode()
        stamp = str(int(time.time()))
        return {'x-kobby-timestamp': stamp, 'x-kobby-event-id': eid,
                'x-kobby-signature': signature('s'*40, stamp, raw)}, raw

    def ready(self, number=1, owner='alice'):
        headers, raw = self.event(number, owner)
        self.assertEqual(503, self.bridge.connect('alexavni', headers, raw))
        self.bridge.tick()
        self.assertEqual(204, self.bridge.connect('alexavni', headers, raw))
        return self.store.one('SELECT * FROM sources')

    def webhook(self, mid=11, text='New post', event='event-11'):
        raw = json.dumps({'type':'message.created','event_id':event,'channel_id':100,
                          'message':{'id':mid,'text':text}}).encode()
        stamp = str(int(time.time()))
        return {'x-webhook-timestamp':stamp,'x-event-id':event,
                'x-webhook-signature':signature('w'*40, stamp, raw)}, raw

    def restart(self):
        self.store.close()
        self.store = Store(self.tmp.name)
        self.bridge = Bridge(self.config, self.store, self.kobby, self.watcher)

    def make_due(self):
        self.store.execute('UPDATE connections SET next_at=0')
        self.store.execute('UPDATE outbox SET next_at=0')

    def test_first_and_second_subscriber_share_channel_and_no_history(self):
        self.ready()
        self.ready(2, 'bob')
        self.assertEqual(1, len(self.kobby.channels))
        self.assertEqual(2, len(self.kobby.joins))
        self.assertEqual(1, len(self.watcher.hooks))
        self.assertEqual([], self.store.rows('SELECT * FROM outbox'))
        self.assertEqual(204, self.bridge.telegram(100, *self.webhook()))
        self.bridge.tick()
        self.assertEqual(1, len(self.kobby.sent))

    def test_concurrent_connections_and_rotation_do_not_duplicate_mirror(self):
        first, second = self.event(), self.event(2, 'bob')
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda e:self.bridge.connect('alexavni', *e), [first, second]))
        # A busy handler returns 503; Kobby redelivers it.
        self.bridge.connect('alexavni', *first)
        self.bridge.connect('alexavni', *second)
        self.bridge.tick()
        self.assertEqual(1, len(self.kobby.channels))
        rotation = self.event(3, 'alice', bot_number=1)
        self.kobby.credentials.pop(f'{1:064x}')
        self.bridge.connect('alexavni', *rotation)
        self.bridge.tick()
        self.assertEqual(1, len(self.kobby.channels))
        self.assertEqual(2, len(self.kobby.joins))
        self.assertEqual(401, self.bridge.connect('alexavni', *first))

    def test_lost_create_response_recovers_same_id_after_restart(self):
        self.kobby.lose_create = True
        event = self.event()
        self.bridge.connect('alexavni', *event)
        self.bridge.tick()
        saved = self.store.one('SELECT * FROM sources')['create_body']
        self.restart()
        self.make_due()
        self.bridge.tick()
        self.assertEqual(saved, self.store.one('SELECT * FROM sources')['create_body'])
        self.assertEqual(1, len(self.kobby.channels))
        self.assertEqual(204, self.bridge.connect('alexavni', *event))

    def test_lost_send_response_and_restart_reuse_exact_request(self):
        self.ready()
        self.bridge.telegram(100, *self.webhook())
        self.kobby.lose_send = True
        self.bridge.tick()
        saved = self.store.one('SELECT * FROM outbox')['body']
        self.restart()
        self.make_due()
        self.bridge.tick()
        self.assertEqual(1, len(self.kobby.sent))
        row = self.store.one('SELECT * FROM outbox')
        self.assertEqual('sent', row['state'])
        self.assertEqual(saved, row['body'])

    def test_pending_queue_survives_restart_and_edits_do_not_change_retry(self):
        self.ready()
        self.bridge.telegram(100, *self.webhook())
        original = self.store.one('SELECT * FROM outbox')['body']
        self.restart()
        self.bridge.telegram(100, *self.webhook(text='Edited post'))
        self.bridge.tick()
        self.assertEqual(original, self.store.one('SELECT * FROM outbox')['body'])

    def test_unavailable_source_does_not_create_channel_or_confirm_connection(self):
        event = self.event()
        self.bridge.connect('alexavni', *event)
        self.watcher.failure = APIError(400, 'source_unavailable')
        self.bridge.tick()
        self.assertEqual({}, self.kobby.channels)
        self.assertNotEqual(204, self.bridge.connect('alexavni', *event))
        self.assertEqual('action_required', self.store.one('SELECT * FROM connections')['state'])

    def test_watcher_authorization_failure_keeps_queue(self):
        self.ready()
        self.bridge.telegram(100, *self.webhook())
        self.watcher.failure = APIError(503, 'authorization_required')
        self.store.execute('UPDATE sources SET last_poll=0')
        self.bridge.tick()
        self.assertEqual(1, len(self.kobby.sent))
        self.assertEqual('authorization_required', self.store.one('SELECT * FROM sources')['error'])

    def test_expired_publisher_reauthenticates_and_failed_login_preserves_job(self):
        self.ready()
        self.bridge.telegram(100, *self.webhook())
        self.kobby.expire_user = self.kobby.login_denied = True
        self.bridge.tick()
        self.assertEqual('pending', self.store.one('SELECT * FROM outbox')['state'])
        self.restart()
        self.make_due()
        self.kobby.login_denied = False
        self.bridge.tick()
        self.assertEqual(1, len(self.kobby.sent))
        self.assertEqual('renewed-user-token', self.store.secret('publisher')['token'])

    def test_revoked_join_permission_stops_business_actions(self):
        self.bridge.connect('alexavni', *self.event())
        self.kobby.credentials[f'{1:064x}']['permissions'] = ['channels.create']
        self.bridge.tick()
        self.assertEqual({}, self.kobby.channels)
        self.assertEqual('bot_permission_required', self.store.one('SELECT * FROM connections')['error'])

    def test_extra_permissions_and_chat_access_do_not_prevent_connection(self):
        grants = ['communities.join', 'contacts.create', 'chats.create', 'groups.create',
                  'channels.create', 'audio.read']
        scopes = [{}, {'chat_ids': ['test-chat']}, {'all_personal_chats': True}]
        for number, scope in enumerate(scopes, 1):
            with self.subTest(scope=scope):
                event = self.event(number, f'test-user-{number}', permissions=grants, **scope)
                self.assertEqual(503, self.bridge.connect('alexavni', *event))
                self.bridge.tick()
                self.assertEqual(204, self.bridge.connect('alexavni', *event))
        self.assertEqual(1, len(self.kobby.channels))
        self.assertEqual(3, len(self.kobby.joins))
        self.bridge.telegram(100, *self.webhook())
        self.bridge.tick()
        self.assertEqual(1, len(self.kobby.sent))
        for _, path, _, token in self.kobby.requests:
            if token and token.startswith('bot-token-'):
                self.assertIn(path, ('/api/v1/bot/me', '/api/v1/bot/communities/join'))
            elif path == '/api/v1/messages':
                self.assertEqual('user-token', token)

    def test_missing_join_permission_is_rejected_even_with_extra_permissions(self):
        for number, permissions in enumerate(([], ['contacts.create', 'channels.create']), 1):
            event = self.event(number, permissions=permissions, all_personal_chats=True)
            self.assertEqual(403, self.bridge.connect('alexavni', *event))
        self.assertEqual([], self.store.rows('SELECT * FROM connections'))
        self.assertEqual({}, self.kobby.channels)
        self.assertEqual(set(), self.kobby.joins)

    def test_extra_permissions_still_require_matching_signed_identity_and_scope(self):
        for number, field, value in ((1, 'owner_id', 'someone-else'),
                                     (2, 'permissions', ['communities.join']),
                                     (3, 'chat_ids', []), (4, 'all_personal_chats', False)):
            with self.subTest(field=field):
                headers, raw = self.event(number, permissions=['communities.join', 'contacts.create'],
                                          chat_ids=['test-chat'], all_personal_chats=True)
                # The signed payload and exchanged credentials must still agree.
                self.kobby.credentials[f'{number:064x}'][field] = value
                self.assertEqual(401, self.bridge.connect('alexavni', headers, raw))
        self.assertEqual([], self.store.rows('SELECT * FROM connections'))

    def test_message_webhooks_are_authenticated_and_discarded(self):
        self.ready()
        stamp = str(int(time.time()))
        event = {'version': 1, 'event': 'message.created', 'event_id': 'f'*64,
                 'bot_id': f'{1:032x}', 'owner_id': 'alice', 'chat_id': 'test-chat',
                 'text': 'Synthetic message that must not be stored'}

        def send(payload, username='alexavni', secret='s'*40, timestamp=stamp):
            raw = json.dumps(payload).encode()
            headers = {'x-kobby-timestamp': timestamp, 'x-kobby-event-id': payload['event_id'],
                       'x-kobby-signature': signature(secret, timestamp, raw)}
            return self.bridge.connect(username, headers, raw)

        requests = len(self.kobby.requests)
        changes = self.store.db.total_changes
        self.assertEqual(204, send(event))
        self.assertEqual(204, send(event))
        self.assertEqual(401, send(event, secret='wrong-secret'))
        self.assertEqual(401, send(event, username='otherchannel'))
        self.assertEqual(401, send(event, timestamp='1'))
        self.assertEqual(401, send(dict(event, owner_id='someone-else')))
        self.assertEqual(401, send(dict(event, bot_id=f'{2:032x}')))
        self.assertEqual(400, send(dict(event, event='unsupported.event')))
        self.assertEqual(changes, self.store.db.total_changes)
        self.assertEqual(requests, len(self.kobby.requests))
        self.assertEqual([], self.store.rows('SELECT * FROM posts'))
        self.assertEqual([], self.store.rows('SELECT * FROM outbox'))

    def test_missing_or_deleted_mirror_is_not_recreated(self):
        self.ready()
        self.kobby.channels.clear()
        self.bridge.telegram(100, *self.webhook())
        self.bridge.tick()
        self.assertEqual('mirror_unavailable', self.store.one('SELECT * FROM sources')['state'])
        self.bridge.tick()
        self.assertEqual({}, self.kobby.channels)

    def test_long_post_utf8_parts_and_media_placeholder(self):
        source = self.ready()
        bodies = render(source, {'id':11,'text':'Привет 👩🏽‍💻\n'*12000}, 'tlg_bridge', self.config['kobby_base'])
        self.assertGreater(len(bodies), 1)
        ids = set()
        for body in bodies:
            obj = json.loads(body)
            text = base64.b64decode(obj['content']['body']).decode()
            self.assertLessEqual(len(text.encode()), 48*1024)
            self.assertIn('https://t.me/alexavni/11', text)
            ids.add(obj['client_message_id'])
        self.assertEqual(len(bodies), len(ids))
        self.bridge.telegram(100, *self.webhook(text=''))
        text = base64.b64decode(json.loads(self.store.one('SELECT body FROM outbox')['body'])['content']['body']).decode()
        self.assertIn('Публикация без текста', text)

    def test_signatures_identity_and_out_of_order_webhooks(self):
        headers, raw = self.event()
        headers['x-kobby-signature'] = 'sha256=bad'
        self.assertEqual(401, self.bridge.connect('alexavni', headers, raw))
        self.assertEqual([], self.store.rows('SELECT * FROM connections'))
        self.ready()
        h, raw = self.webhook()
        h['x-webhook-timestamp'] = '1'
        self.assertEqual(401, self.bridge.telegram(100,h,raw))
        self.bridge.telegram(100, *self.webhook(12,event='event-12'))
        self.bridge.telegram(100, *self.webhook(11,event='event-11'))
        self.bridge.tick()
        self.bridge.tick()
        sent = list(self.kobby.sent.values())
        self.assertEqual(2, len(sent))
        self.assertIn('/11', base64.b64decode(sent[0]['content']['body']).decode())

    def test_encryption_and_database_binding(self):
        self.ready()
        with self.assertRaises(ValueError):
            self.store.bind('kobby_base','https://untrusted.example')
        self.store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        raw = (Path(self.tmp.name)/'state.sqlite').read_bytes()
        for secret in (b'user-token', b'test-only-generated-password', b'bot-token-1', b'watcher-key'):
            self.assertNotIn(secret,raw)

    def test_http_route_and_health(self):
        with TestClient(create_app(bridge=self.bridge, worker=False)) as client:
            self.assertEqual(200, client.get('/healthz').status_code)
            self.assertEqual(200, client.get('/t.me/alexavni').status_code)
            self.assertEqual(404, client.get('/telegram/alexavni').status_code)
            headers, raw = self.event()
            self.assertEqual(503, client.post('/t.me/alexavni', content=raw, headers=headers).status_code)
            self.bridge.tick()
            self.assertEqual(204, client.post('/t.me/alexavni', content=raw, headers=headers).status_code)


if __name__ == '__main__':
    unittest.main()
