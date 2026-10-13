import contextlib
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import threading

from cryptography.fernet import Fernet


class Store:
    def __init__(self, directory, *, create_key=False, key_path=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.lockfile = open(self.directory / 'process.lock', 'a')
        fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        keypath = Path(key_path) if key_path else self.directory / 'master.key'
        if create_key and not keypath.exists():
            if (self.directory / 'state.sqlite').exists():
                raise RuntimeError('Restore the original master.key; database already exists')
            with keypath.open('xb') as file:
                os.chmod(keypath, 0o600)
                file.write(Fernet.generate_key())
                file.flush()
                os.fsync(file.fileno())
        self.cipher = Fernet(keypath.read_bytes())
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory / 'state.sqlite', check_same_thread=False)
        os.chmod(self.directory / 'state.sqlite', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS secrets(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS connections(
                bot_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, username TEXT NOT NULL,
                event_id TEXT NOT NULL, credentials TEXT NOT NULL, source_id INTEGER,
                state TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS exchanges(
                event_id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, username TEXT NOT NULL,
                exchange_id TEXT NOT NULL, credentials TEXT, done INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sources(
                id INTEGER PRIMARY KEY, username TEXT NOT NULL, title TEXT NOT NULL,
                channel_id TEXT NOT NULL UNIQUE, create_body TEXT NOT NULL,
                baseline INTEGER NOT NULL, cursor INTEGER NOT NULL,
                created INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'creating', error TEXT NOT NULL DEFAULT '',
                webhook_secret TEXT, webhook_id TEXT, last_poll REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS posts(
                source_id INTEGER NOT NULL REFERENCES sources(id), message_id INTEGER NOT NULL,
                body TEXT NOT NULL, PRIMARY KEY(source_id,message_id));
            CREATE TABLE IF NOT EXISTS inbox(
                event_id TEXT PRIMARY KEY, source_id INTEGER NOT NULL, message_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(
                id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
                message_id INTEGER NOT NULL, part INTEGER NOT NULL, body TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                UNIQUE(source_id,message_id,part));
        ''')
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(sources)')}
        if 'created' not in columns:
            self.db.execute('ALTER TABLE sources ADD COLUMN created INTEGER NOT NULL DEFAULT 0')
            self.db.commit()

    def seal(self, value):
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()

    def unseal(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def secret(self, key):
        row = self.one('SELECT value FROM secrets WHERE key=?', (key,))
        return self.unseal(row['value']) if row else None

    def put_secret(self, key, value):
        self.execute('INSERT INTO secrets VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (key, self.seal(value)))

    @contextlib.contextmanager
    def transaction(self):
        with self.lock, self.db:
            yield self.db

    def execute(self, sql, params=()):
        with self.transaction() as db:
            return db.execute(sql, params)

    def one(self, sql, params=()):
        with self.lock:
            row = self.db.execute(sql, params).fetchone()
            return dict(row) if row else None

    def rows(self, sql, params=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, params)]

    def bind(self, key, value):
        with self.transaction() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            if row and row['value'] != value:
                raise ValueError('Database belongs to another ' + key)
            db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', (key, value))

    def close(self):
        self.db.close()
        self.lockfile.close()
