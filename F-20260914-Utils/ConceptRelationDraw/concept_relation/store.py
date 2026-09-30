import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = root / "jobs.sqlite3"
        with self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.db, timeout=30)

    def create(self, filename, data):
        job_id = uuid.uuid4().hex
        folder = self.root / job_id
        folder.mkdir()
        (folder / "source").write_bytes(data)
        job = dict(job_id=job_id, filename=filename, status="uploaded", completed_stages=[], progress=0,
                   message="文档已上传", error=None, created_at=now(), updated_at=now(), counts={})
        self.save(job)
        return job

    def save(self, job):
        job["updated_at"] = now()
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO jobs VALUES (?,?)", (job["job_id"], json.dumps(job, ensure_ascii=False)))

    def get(self, job_id):
        with self.connect() as conn:
            row = conn.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        return json.loads(row[0])

    def list(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT body FROM jobs ORDER BY rowid DESC LIMIT 100").fetchall()
        return [json.loads(row[0]) for row in rows]

    def path(self, job_id, filename):
        self.get(job_id)
        return self.root / job_id / filename

    def write(self, job_id, filename, value):
        path = self.path(job_id, filename)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        tmp.replace(path)

    def read(self, job_id, filename):
        return json.loads(self.path(job_id, filename).read_text(encoding="utf-8"))

    def recover(self):
        with self.connect() as conn:
            jobs = [json.loads(row[0]) for row in conn.execute("SELECT body FROM jobs").fetchall()]
        for job in jobs:
            if job["status"] not in ("uploaded", "completed", "failed"):
                job.update(status="failed", error="服务曾中断，请重新上传文档启动新任务。", message="任务中断")
                self.save(job)

    def has_active_jobs(self):
        with self.connect() as conn:
            return any(json.loads(row[0])["status"] not in ("uploaded", "completed", "failed")
                       for row in conn.execute("SELECT body FROM jobs"))
