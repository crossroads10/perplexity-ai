"""Bounded durable response receipts. Never a cache for unscoped prompts."""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .job_store import JobError


class ReceiptStore:
    def __init__(self, path=None):
        if path:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(target, 0o600)
        self.db = sqlite3.connect(str(path) if path else ':memory:', check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute('CREATE TABLE IF NOT EXISTS receipts (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL)')
        self.db.commit()

    def get(self, key, fingerprint):
        with self.lock:
            return self._get(key, fingerprint)

    def _get(self, key, fingerprint):
        row = self.db.execute('SELECT fingerprint,payload,created FROM receipts WHERE key=?', (key,)).fetchone()
        if not row or row[2] < time.time()-86400:
            return None
        if row[0] != fingerprint:
            raise JobError('Idempotency key was reused with different input', 'idempotency_conflict', 409)
        return json.loads(row[1])

    def put(self, key, fingerprint, payload):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                previous = self._get(key, fingerprint)
                if previous is not None:
                    self.db.commit()
                    return previous
                self.db.execute('DELETE FROM receipts WHERE created < ?', (time.time()-86400,))
                self.db.execute('INSERT OR REPLACE INTO receipts VALUES (?,?,?,?)',
                                (key, fingerprint, json.dumps(payload, ensure_ascii=False), time.time()))
                self.db.execute('DELETE FROM receipts WHERE key IN (SELECT key FROM receipts ORDER BY created DESC LIMIT -1 OFFSET 512)')
                self.db.commit()
                return payload
            except BaseException:
                self.db.rollback()
                raise

    def close(self):
        with self.lock:
            self.db.close()
