import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = self.data_dir / "jobs.sqlite3"
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, lease REAL,
                request TEXT NOT NULL, error TEXT)""")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def job_dir(self, job_id):
        return self.data_dir / "jobs" / job_id

    def enqueue(self, job_id, request):
        now = time.time()
        with self.connect() as conn:
            conn.execute("INSERT INTO jobs VALUES (?, 'queued', 'queued', ?, ?, NULL, ?, NULL)",
                         (job_id, now, now, json.dumps(request, ensure_ascii=False)))

    def get(self, job_id):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["request"] = json.loads(data["request"])
        return data

    def claim(self, lease_seconds):
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""UPDATE jobs SET status='failed', stage='failed', updated=?,
                         error='Worker 连接中断；请重新提交任务' WHERE status='running' AND lease<?""", (now, now))
            row = conn.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            conn.execute("UPDATE jobs SET status='running', stage='starting', updated=?, lease=? WHERE id=?",
                         (now, now + lease_seconds, row[0]))
        return self.get(row[0])

    def heartbeat(self, job_id, lease_seconds):
        with self.connect() as conn:
            return conn.execute("UPDATE jobs SET updated=?, lease=? WHERE id=? AND status='running'",
                                (time.time(), time.time() + lease_seconds, job_id)).rowcount == 1

    def stage(self, job_id, stage):
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET stage=?, updated=? WHERE id=? AND status='running'",
                         (stage, time.time(), job_id))

    def finish(self, job_id, error=None):
        status = "failed" if error else "succeeded"
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET status=?, stage=?, updated=?, lease=NULL, error=? WHERE id=? AND status='running'",
                         (status, status, time.time(), error, job_id))
