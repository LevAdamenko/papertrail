"""Persistent session authorization and atomic live-run limits (stdlib only)."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import secrets
import sqlite3
from pathlib import Path

TTL = 30 * 24 * 60 * 60


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Access:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'live.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (owner TEXT PRIMARY KEY, expires REAL, unlocked INTEGER);
                CREATE TABLE IF NOT EXISTS failures (ip TEXT, at REAL);
                CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, runs INTEGER);
            ''')
            db.execute('DELETE FROM sessions WHERE expires <= ?', (self.timestamp(),))

    @staticmethod
    def timestamp():
        return datetime.now(timezone.utc).timestamp()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def session(self, token):
        if not token or len(token) > 128:
            return None
        with self.connect() as db:
            row = db.execute('SELECT owner, unlocked FROM sessions WHERE owner=? AND expires>?',
                             (token_hash(token), self.timestamp())).fetchone()
        return row

    def create(self, unlocked=False):
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('INSERT INTO sessions VALUES (?, ?, ?)',
                       (token_hash(token), self.timestamp() + TTL, int(unlocked)))
        return token

    def attempt(self, ip, valid):
        """Serialize the check and increment so concurrent attempts cannot bypass it."""
        stamp = self.timestamp()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM failures WHERE at <= ?', (stamp - 3600,))
            count = db.execute('SELECT count(*) FROM failures WHERE ip=?', (token_hash(ip),)).fetchone()[0]
            if count >= 10:
                return False
            if not valid:
                db.execute('INSERT INTO failures VALUES (?, ?)', (token_hash(ip), stamp))
        return True

    def reserve(self, cap):
        day = datetime.now(timezone.utc).date().isoformat()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO usage VALUES (?, 0)', (day,))
            return db.execute('UPDATE usage SET runs=runs+1 WHERE day=? AND runs<?', (day, cap)).rowcount == 1
