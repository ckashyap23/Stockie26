"""Single-host subprocess jobs with persistent status and serialized writers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from uuid import uuid4

from .runtime import output_dir as service_output_dir, child_environment
from .tasks import task_module


class JobBusyError(RuntimeError):
    pass


class JobStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or service_output_dir() / "jobs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / "jobs.sqlite3"
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS job (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, args TEXT NOT NULL,
                state TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT, exit_code INTEGER, error TEXT)""")
            # Atomic exclusion works even for simultaneous HTTP requests.
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_writer ON job ((1)) WHERE state='running'")

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def submit(self, task: str, args: list[str]) -> dict:
        module = task_module(task)
        if not isinstance(args, list) or len(args) > 64 or any(
            not isinstance(arg, str) or len(arg) > 2048 or "\x00" in arg for arg in args
        ):
            raise ValueError("args must be a list of up to 64 command-line strings")
        job_id = uuid4().hex
        try:
            with self._connect() as db:
                db.execute("INSERT INTO job(id,task,args,state) VALUES(?,?,?,'running')",
                           (job_id, task, json.dumps(args)))
        except sqlite3.IntegrityError:
            raise JobBusyError("A task is already running; wait for it to finish") from None
        try:
            thread = threading.Thread(target=self._run, args=(job_id, module, list(args)), daemon=True)
            thread.start()
        except Exception as exc:
            self._finish(job_id, None, str(exc))
            raise
        return self.get(job_id)

    def _finish(self, job_id: str, code: int | None, error: str | None = None):
        with self._connect() as db:
            db.execute("UPDATE job SET state=?,exit_code=?,error=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                       ("done" if code == 0 else "failed", code, error, job_id))

    def _run(self, job_id: str, module: str, args: list[str]):
        try:
            env = child_environment()
            service_output_dir().mkdir(parents=True, exist_ok=True)
            with (self.directory / f"{job_id}.log").open("w", encoding="utf-8") as log:
                result = subprocess.run([sys.executable, "-m", module, *args],
                                        stdout=log, stderr=subprocess.STDOUT,
                                        env=env, cwd=service_output_dir(), check=False)
            self._finish(job_id, result.returncode)
        except Exception as exc:
            self._finish(job_id, None, str(exc))

    def get(self, job_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["args"] = json.loads(item["args"])
        log = self.directory / f"{item['id']}.log"
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 16000))
                item["log_tail"] = stream.read().decode("utf-8", errors="replace")
        else:
            item["log_tail"] = ""
        return item

    def list(self) -> list[dict]:
        with self._connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM job ORDER BY created_at DESC, rowid DESC LIMIT 100")]
        return [self.get(job_id) for job_id in ids]
