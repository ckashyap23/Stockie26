"""Hosted scheduler: runs the daily job roster at its IST times from ONE always-on process (Railway has no per-job time zone, and the
roster is ~20 jobs, so one scheduler service beats 20 cron services that would each rebuild this image).

Each firing launches `python -m technical_analysis_service.scheduled_jobs --job <id>` in a subprocess, which already provides the
per-job IST windows, NSE-holiday guard, same-day dependency checks, redacted logs and status files. This module only decides WHEN.

    python -m technical_analysis_service scheduler              # standalone
    TECHNICAL_ANALYSIS_SCHEDULER=1 ... serve                    # inside the web service (one Railway service, one Volume)

Times come from scheduled_jobs.JOBS (each job fires at the start of its window); paper-monitor repeats every 5 minutes from 09:30 to
15:25 plus a final 15:29 reconciliation. Weekends are skipped here; NSE holidays are skipped by the job runner (it checks TradingCalendar).
Off unless enabled: the Windows Task Scheduler runs the same jobs today, and two schedulers would place duplicate paper trades.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .scheduled_jobs import JOBS

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
MONITOR = "paper-monitor"


def monitor_minutes() -> set[str]:
    times = {f"{h:02d}:{m:02d}" for h in range(9, 16) for m in range(0, 60, 5) if "09:30" <= f"{h:02d}:{m:02d}" <= "15:25"}
    return times | {"15:29"}


def schedule() -> dict[str, set[str]]:
    """job id -> set of HH:MM (IST) when it fires on a weekday."""
    table = {job: {definition[3]} for job, definition in JOBS.items() if job != MONITOR}
    table[MONITOR] = monitor_minutes()
    return table


def due_jobs(now: datetime) -> list[str]:
    """Jobs due in this minute (IST). Pure function, so it is easy to test."""
    now = now.astimezone(IST)
    if now.weekday() >= 5:
        return []
    stamp = f"{now.hour:02d}:{now.minute:02d}"
    return [job for job, times in schedule().items() if stamp in times]


class Scheduler:
    def __init__(self, workspace: Path, clock=lambda: datetime.now(IST), launch=None) -> None:
        self.workspace = Path(workspace)
        self._clock = clock
        self._launch = launch or self._spawn
        self._last: tuple[int, int, int, int] | None = None
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _spawn(self, job: str) -> int:
        command = [sys.executable, "-u", "-m", "technical_analysis_service.scheduled_jobs", "--workspace", str(self.workspace), "--job", job]
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1500)
        lines = (done.stdout or "").strip().splitlines()
        log.info("job %s exit %s: %s", job, done.returncode, lines[-1][:400] if lines else "")  # the runner prints its (redacted) final state
        return done.returncode

    def _run(self, job: str) -> None:
        try:
            self._launch(job)
        except Exception:  # noqa: BLE001 - one failing job must not stop the scheduler
            log.exception("job %s could not run", job)
        finally:
            with self._lock:
                self._running.discard(job)

    def tick(self) -> list[str]:
        """Start whatever is due this minute (once per minute; a job still running is not started twice). Returns the jobs started."""
        now = self._clock()
        stamp = (now.month, now.day, now.hour, now.minute)
        if stamp == self._last:
            return []
        self._last = stamp
        started = []
        for job in due_jobs(now):
            with self._lock:
                if job in self._running:
                    log.warning("job %s still running from the previous trigger; skipped", job)
                    continue
                self._running.add(job)
            threading.Thread(target=self._run, args=(job,), name=f"job-{job}", daemon=True).start()
            started.append(job)
        return started

    def run_forever(self) -> None:
        log.info("technical-analysis scheduler on (workspace %s, %d jobs)", self.workspace, len(JOBS))
        while not self._stop.wait(15):
            self.tick()

    def start_background(self) -> None:
        threading.Thread(target=self.run_forever, name="ta-scheduler", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def default_workspace() -> Path:
    """Where the job runner keeps the Kite token file and output/ (a Volume on Railway: /data)."""
    return Path(os.getenv("TECHNICAL_ANALYSIS_WORKSPACE", os.getenv("DATA_DIR", "/data" if Path("/data").exists() else "."))).resolve()


def enabled() -> bool:
    return os.getenv("TECHNICAL_ANALYSIS_SCHEDULER", "").strip().lower() in {"1", "true", "yes", "on"}
