"""In-process scheduler for a hosted instance (Railway runs ONE service; no separate cron services needed).

  - every `schedule_ingest_minutes` (default 30): fetch the feeds into the archive (every day, so Monday's window has the weekend);
  - Monday-Friday at `schedule_run_time` IST (default 08:30): start the day's run (publishes the score to NiftyPrediction.news_score).

Off unless SCHEDULER_ENABLED=1. Uses the same JobManager as the API, so a manual run and a scheduled run never overlap.
Exchange holidays are not modelled: a run on a holiday still scores, but publishes nothing (no prediction row has that trade date).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.jobs import JobBusy, JobManager

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def due_actions(now: datetime, settings: Settings) -> list[str]:
    """Which actions are due in this minute (IST). Pure function, so it is easy to test."""
    now = now.astimezone(IST)
    actions = []
    if (now.hour * 60 + now.minute) % max(1, settings.schedule_ingest_minutes) == 0:
        actions.append("ingest")
    run_at = settings.schedule_run_time
    if now.weekday() < 5 and (now.hour, now.minute) == (run_at.hour, run_at.minute):
        actions.append("run")
    return actions


class Scheduler:
    def __init__(self, settings: Settings, jobs: JobManager, ingest=None, clock=lambda: datetime.now(IST)) -> None:
        from app.pipeline.ingest_job import run_ingest

        self.settings, self.jobs, self._clock = settings, jobs, clock
        self._ingest = ingest or run_ingest
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: tuple[int, int, int, int] | None = None

    def tick(self) -> list[str]:
        """Run whatever is due now (at most once per minute). Returns the actions started."""
        now = self._clock()
        stamp = (now.month, now.day, now.hour, now.minute)
        if stamp == self._last:
            return []
        self._last = stamp
        started = []
        for action in due_actions(now, self.settings):
            try:
                if action == "ingest":
                    result = self._ingest(self.settings)
                    log.info("scheduled ingest: %s new of %s fetched", result.get("new"), result.get("fetched"))
                else:
                    job = self.jobs.submit(self.settings, now.date())
                    log.info("scheduled run for %s started: %s", now.date(), job["job_id"])
                started.append(action)
            except JobBusy as busy:
                log.warning("scheduled %s skipped: a run is already in progress (%s)", action, busy.job.get("job_id"))
            except Exception:  # noqa: BLE001 - a failed feed fetch must not kill the scheduler
                log.exception("scheduled %s failed", action)
        return started

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(15):
                self.tick()

        self._thread = threading.Thread(target=loop, name="news-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler on: ingest every %s min, run Mon-Fri %s IST", self.settings.schedule_ingest_minutes, self.settings.schedule_run_time)

    def stop(self) -> None:
        self._stop.set()
