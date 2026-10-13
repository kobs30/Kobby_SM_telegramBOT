import hashlib
import hmac
import json
import logging
import random
import re
import threading
import time
import uuid

from bridge.api import APIError, Publisher
from bridge.render import USERNAME, render

log = logging.getLogger('bridge')
HEX32 = re.compile('[0-9a-f]{32}\\Z')
HEX64 = re.compile('[0-9a-f]{64}\\Z')


def signature(secret, timestamp, raw):
    return 'sha256=' + hmac.new(secret.encode(), timestamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()


def timestamp_ok(stamp):
    try:
        return abs(time.time() - int(stamp)) <= 300
    except (ValueError, TypeError):
        return False


class Bridge:
    def __init__(self, config, store, kobby, watcher):
        self.config, self.store, self.kobby, self.watcher = config, store, kobby, watcher
        self.publisher = Publisher(store, kobby)
        self.work_lock = threading.Lock()
        self.stop = threading.Event()
        self.last_tick = time.time()
        self.worker_error = ''
        self.publisher_retry_at = 0
        store.bind('kobby_base', config['kobby_base'])
        store.bind('watcher_base', config['watcher_base'])

    def connect(self, username, headers, raw):
        username = username.lower()
        if not USERNAME.fullmatch(username):
            return 400
        stamp = headers.get('x-kobby-timestamp', '')
        if not timestamp_ok(stamp):
            return 401
        try:
            event = json.loads(raw)
            if (event['version'] != 1 or event['event'] != 'bot.connected'
                    or not HEX64.fullmatch(event['event_id']) or not HEX32.fullmatch(event['bot_id'])
                    or not HEX64.fullmatch(event['connection_code'])
                    or headers.get('x-kobby-event-id') != event['event_id']):
                return 400
        except (ValueError, KeyError, TypeError):
            return 400
        if not self.work_lock.acquire(blocking=False):
            return 503
        try:
            row = self.store.one('SELECT * FROM exchanges WHERE event_id=?', (event['event_id'],))
            if not row:
                self.store.execute('INSERT INTO exchanges(event_id,bot_id,username,exchange_id) VALUES(?,?,?,?)',
                                   (event['event_id'], event['bot_id'], username, uuid.uuid4().hex))
                row = self.store.one('SELECT * FROM exchanges WHERE event_id=?', (event['event_id'],))
            if row['bot_id'] != event['bot_id'] or row['username'] != username:
                return 409
            credentials = self.store.unseal(row['credentials']) if row['credentials'] else self.kobby.request(
                'POST', '/api/v1/bot/connect', {'bot_id': event['bot_id'], 'event_id': event['event_id'],
                                              'code': event['connection_code'], 'exchange_id': row['exchange_id']})
            if not hmac.compare_digest(signature(credentials['webhook_secret'], stamp, raw), headers.get('x-kobby-signature', '')):
                return 401
            for field in ('bot_id', 'owner_id', 'event_id', 'permissions', 'all_personal_chats', 'chat_ids'):
                if credentials.get(field) != event.get(field):
                    return 401
            if (set(credentials['permissions']) != {'communities.join'}
                    or credentials['chat_ids'] or credentials['all_personal_chats']):
                return 403
            # Check current credentials before allowing an old signed connection
            # event to overwrite a later rotation or source change.
            me = self.kobby.request('GET', '/api/v1/bot/me', token=credentials['api_token'])
            for field in ('bot_id', 'owner_id', 'permissions', 'all_personal_chats', 'chat_ids'):
                if me.get(field) != credentials.get(field):
                    return 401
            if row['done']:
                return 204
            sealed = self.store.seal(credentials)
            with self.store.transaction() as db:
                db.execute('UPDATE exchanges SET credentials=? WHERE event_id=?', (sealed, event['event_id']))
                old = db.execute('SELECT * FROM connections WHERE bot_id=?', (event['bot_id'],)).fetchone()
                if not old or old['event_id'] != event['event_id']:
                    db.execute('''INSERT INTO connections(bot_id,owner_id,username,event_id,credentials)
                        VALUES(?,?,?,?,?) ON CONFLICT(bot_id) DO UPDATE SET
                        owner_id=excluded.owner_id,username=excluded.username,event_id=excluded.event_id,
                        credentials=excluded.credentials,source_id=NULL,state='pending',error='',attempts=0,next_at=0''',
                               (event['bot_id'], credentials['owner_id'], username, event['event_id'], sealed))
                elif old['state'] == 'joined':
                    db.execute('UPDATE exchanges SET done=1 WHERE event_id=?', (event['event_id'],))
                    return 204
            # Background work is durable, but keep Kobby in connecting until join
            # succeeds. Retries only check completion; they never recreate objects.
            return 503
        except APIError as error:
            return 503 if error.status >= 500 or error.status == 429 else error.status
        except (ValueError, KeyError, TypeError):
            return 400
        finally:
            self.work_lock.release()

    def watcher_request(self, method, path, body=None):
        key = self.store.secret('watcher_key')
        if not key:
            raise APIError(503, 'watcher_key_required')
        return self.watcher.request(method, path, body, key)

    def source_for(self, connection):
        if connection['source_id']:
            return self.store.one('SELECT * FROM sources WHERE id=?', (connection['source_id'],))
        channel = self.watcher_request('POST', '/v1/channels', {'url': 'https://t.me/' + connection['username']})
        cid = channel.get('id')
        if not isinstance(cid, int) or isinstance(cid, bool) or cid <= 0 or not USERNAME.fullmatch(channel.get('username', '')):
            raise APIError(502, 'invalid_watcher_channel')
        if channel.get('status') != 'active':
            raise APIError(503, 'source_unavailable')
        # Verify history is readable before creating a Kobby mirror.
        recent = self.watcher_request('GET', f'/v1/channels/{cid}/messages')
        if not isinstance(recent.get('items'), list):
            raise APIError(502, 'invalid_watcher_history')
        with self.store.transaction() as db:
            source = db.execute('SELECT * FROM sources WHERE id=?', (cid,)).fetchone()
            if not source:
                baseline = max([int(channel.get('last_seen', 0))] + [m['id'] for m in recent['items']])
                kid = uuid.uuid4().hex
                body = json.dumps({'id': kid, 'type': 'channel', 'title': 'tlg_' + channel['username'],
                                   'description': 'Telegram mirror: https://t.me/' + channel['username']}, sort_keys=True)
                db.execute('''INSERT INTO sources(id,username,title,channel_id,create_body,baseline,cursor)
                              VALUES(?,?,?,?,?,?,?)''',
                           (cid, channel['username'], channel.get('title', channel['username']), kid, body, baseline, baseline))
            db.execute('UPDATE connections SET source_id=? WHERE bot_id=?', (cid, connection['bot_id']))
        return self.store.one('SELECT * FROM sources WHERE id=?', (cid,))

    def ensure_source(self, source):
        if source['state'] == 'mirror_unavailable':
            raise APIError(403, 'mirror_unavailable')
        if not source['created']:
            result = self.publisher.request('POST', '/api/v1/communities/create', json.loads(source['create_body']))
            if result.get('id') != source['channel_id'] or result.get('type') != 'channel':
                raise APIError(502, 'invalid_created_channel')
            self.store.execute('UPDATE sources SET created=1 WHERE id=?', (source['id'],))
        try:
            info = self.publisher.request('GET', '/api/v1/communities/info?id=' + source['channel_id'])
            chat = info.get('chat', {})
            if (chat.get('id') != source['channel_id'] or chat.get('type') != 'channel'
                    or chat.get('visibility') != 'public' or chat.get('viewer_role') not in ('owner', 'admin')):
                raise APIError(403, 'mirror_unavailable')
        except APIError as error:
            if error.status in (403, 404):
                self.store.execute("UPDATE sources SET state='mirror_unavailable',error=? WHERE id=?", (error.code, source['id']))
            raise
        if not source['webhook_secret']:
            hook = self.watcher_request('POST', f"/v1/channels/{source['id']}/webhooks",
                                       {'url': self.config['public_base'] + f"/events/telegram/{source['id']}"})
            if not isinstance(hook.get('secret'), str) or len(hook['secret']) < 32:
                raise APIError(502, 'invalid_webhook_secret')
            self.store.execute('UPDATE sources SET webhook_id=?,webhook_secret=? WHERE id=?',
                               (hook['id'], self.store.seal(hook['secret']), source['id']))
        self.store.execute("UPDATE sources SET state='ready',error='' WHERE id=?", (source['id'],))
        # Reconcile the registration gap, within the watcher's five-post window.
        self.poll_source(self.store.one('SELECT * FROM sources WHERE id=?', (source['id'],)))

    def connect_job(self, row):
        credentials = self.store.unseal(row['credentials'])
        me = self.kobby.request('GET', '/api/v1/bot/me', token=credentials['api_token'])
        if me.get('owner_id') != row['owner_id'] or me.get('bot_id') != row['bot_id']:
            raise APIError(403, 'connection_identity_mismatch')
        if (set(me.get('permissions', [])) != {'communities.join'}
                or me.get('chat_ids') or me.get('all_personal_chats')):
            raise APIError(403, 'bot_permission_required')
        source = self.source_for(row)
        self.ensure_source(source)
        joined = self.kobby.request('POST', '/api/v1/bot/communities/join',
                                   {'id': source['channel_id']}, credentials['api_token'])
        if joined.get('id') != source['channel_id'] or joined.get('viewer_role') not in ('member', 'admin', 'owner'):
            raise APIError(502, 'invalid_join_response')
        self.store.execute("UPDATE connections SET state='joined',error='' WHERE bot_id=?", (row['bot_id'],))

    def ingest(self, source, message, event_id=None):
        mid = message.get('id')
        if not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0:
            raise ValueError('Invalid message ID')
        if mid <= source['baseline']:
            return
        account = self.store.secret('publisher')
        bodies = render(source, message, account['username'], self.config['kobby_base'])
        with self.store.transaction() as db:
            if event_id:
                existing = db.execute('SELECT * FROM inbox WHERE event_id=?', (event_id,)).fetchone()
                if existing and (existing['source_id'] != source['id'] or existing['message_id'] != mid):
                    raise ValueError('Event identity conflict')
                db.execute('INSERT OR IGNORE INTO inbox VALUES(?,?,?)', (event_id, source['id'], mid))
            old = db.execute('SELECT 1 FROM posts WHERE source_id=? AND message_id=?', (source['id'], mid)).fetchone()
            if old:
                return
            db.execute('INSERT INTO posts VALUES(?,?,?)', (source['id'], mid, json.dumps(message, ensure_ascii=False)))
            for part, body in enumerate(bodies, 1):
                db.execute('INSERT INTO outbox(source_id,message_id,part,body) VALUES(?,?,?,?)', (source['id'], mid, part, body))
            db.execute('UPDATE sources SET cursor=MAX(cursor,?) WHERE id=?', (mid, source['id']))

    def telegram(self, channel_id, headers, raw):
        source = self.store.one('SELECT * FROM sources WHERE id=?', (channel_id,))
        if not source or not source['webhook_secret']:
            return 503
        stamp = headers.get('x-webhook-timestamp', '')
        if not timestamp_ok(stamp):
            return 401
        secret = self.store.unseal(source['webhook_secret'])
        if not hmac.compare_digest(signature(secret, stamp, raw), headers.get('x-webhook-signature', '')):
            return 401
        try:
            event = json.loads(raw)
            if (event['type'] != 'message.created' or event['channel_id'] != channel_id
                    or not isinstance(event['event_id'], str) or len(event['event_id']) > 128
                    or not event['event_id'] or event['event_id'] != headers.get('x-event-id')):
                return 400
            self.ingest(source, event['message'], event['event_id'])
            return 204
        except (ValueError, TypeError, KeyError):
            return 400

    def poll_source(self, source):
        # Always inspect the retained window, not only IDs above cursor: webhook
        # delivery may be out of order and ID gaps do not prove message loss.
        result = self.watcher_request('GET', f"/v1/channels/{source['id']}/messages?after_id={source['baseline']}")
        for message in sorted(result['items'], key=lambda m: m['id']):
            self.ingest(source, message)
        self.store.execute('UPDATE sources SET last_poll=? WHERE id=?', (time.time(), source['id']))

    @staticmethod
    def backoff(attempts, error):
        return time.time() + max(error.retry_after, min(900, 2 ** min(attempts + 1, 10)) + random.random())

    def tick(self):
        if not self.work_lock.acquire(blocking=False):
            return
        try:
            now = time.time()
            for row in self.store.rows("SELECT * FROM connections WHERE state IN ('pending','retrying') AND next_at<=? LIMIT 4", (now,)):
                try:
                    self.connect_job(row)
                except APIError as error:
                    state = 'retrying' if error.status >= 500 or error.status == 429 else 'action_required'
                    self.store.execute('UPDATE connections SET state=?,error=?,attempts=attempts+1,next_at=? WHERE bot_id=?',
                                       (state, error.code, self.backoff(row['attempts'], error), row['bot_id']))
                    log.warning('Connection pending: bot=%s code=%s', row['bot_id'], error.code)
            for source in self.store.rows("SELECT * FROM sources WHERE state='ready' AND last_poll<? LIMIT 8", (now - self.config.get('poll_interval', 60),)):
                try:
                    self.poll_source(source)
                except APIError as error:
                    delay = max(0, error.retry_after - self.config.get('poll_interval', 60))
                    self.store.execute('UPDATE sources SET error=?,last_poll=? WHERE id=?', (error.code, now + delay, source['id']))
            if now < self.publisher_retry_at:
                return
            # One earliest unfinished part per source. Other sources can progress
            # while a single mirror is delayed. No volatile in-flight state.
            jobs = self.store.rows('''SELECT o.* FROM outbox o JOIN sources s ON s.id=o.source_id
                WHERE o.state='pending' AND o.next_at<=? AND s.state='ready'
                AND NOT EXISTS(SELECT 1 FROM outbox earlier WHERE earlier.source_id=o.source_id
                    AND earlier.state!='sent' AND (earlier.message_id<o.message_id
                    OR (earlier.message_id=o.message_id AND earlier.part<o.part)))
                ORDER BY o.id LIMIT 16''', (now,))
            for job in jobs:
                try:
                    result = self.publisher.request('POST', '/api/v1/messages', json.loads(job['body']))
                    expected = json.loads(job['body'])
                    message = result.get('message', {})
                    if any(message.get(k) != expected[k] for k in ('chat_id', 'sender_id', 'recipient_id', 'client_message_id', 'content')):
                        raise APIError(502, 'invalid_publication_response')
                    self.store.execute("UPDATE outbox SET state='sent',error='' WHERE id=?", (job['id'],))
                except APIError as error:
                    state = 'pending' if error.status in (401, 429) or error.status >= 500 else 'failed'
                    self.store.execute('UPDATE outbox SET state=?,error=?,attempts=attempts+1,next_at=? WHERE id=?',
                                       (state, error.code, self.backoff(job['attempts'], error), job['id']))
                    if error.status in (403, 404):
                        self.store.execute("UPDATE sources SET state='mirror_unavailable',error=? WHERE id=?", (error.code, job['source_id']))
                    if error.status in (401, 429):
                        self.publisher_retry_at = self.backoff(job['attempts'], error)
                        break
        finally:
            self.last_tick = time.time()
            self.work_lock.release()

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
                self.worker_error = ''
            except Exception as error:
                # Never dump an exception containing API bodies or credentials.
                self.worker_error = type(error).__name__
                log.error('Worker iteration failed: type=%s', self.worker_error)
            self.stop.wait(1)
