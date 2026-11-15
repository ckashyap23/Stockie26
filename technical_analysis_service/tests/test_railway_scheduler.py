from datetime import datetime
from zoneinfo import ZoneInfo

from technical_analysis_service.railway_scheduler import MONITOR, Scheduler, due_jobs, monitor_minutes, schedule
from technical_analysis_service.scheduled_jobs import JOBS

IST = ZoneInfo("Asia/Kolkata")


def at(day, hh, mm):
    return datetime(2026, 11, day, hh, mm, tzinfo=IST)


def test_every_job_in_the_roster_is_scheduled_at_the_start_of_its_window():
    table = schedule()
    assert set(table) == set(JOBS)
    for job, (_, _, _, earliest, _, _) in JOBS.items():
        if job != MONITOR:
            assert table[job] == {earliest}


def test_morning_chain_fires_in_order_on_a_weekday():
    assert due_jobs(at(11, 9, 11)) == ["gift-preopen"]
    assert due_jobs(at(11, 9, 15)) == ["option-snapshot"]
    assert due_jobs(at(11, 9, 24)) == ["prediction"]
    assert due_jobs(at(11, 9, 27)) == ["paper-entry"]
    assert due_jobs(at(11, 9, 12)) == []
    assert due_jobs(at(11, 15, 47)) == ["market-refresh"]


def test_monitor_runs_every_five_minutes_through_the_session_plus_final_reconciliation():
    times = monitor_minutes()
    assert {"09:30", "09:35", "15:10", "15:25", "15:29"} <= times and "09:25" not in times and "15:30" not in times
    assert MONITOR in due_jobs(at(11, 15, 10))  # the 15:10 Streak exit is covered by the 5-minute grid
    assert MONITOR not in due_jobs(at(11, 15, 30))


def test_nothing_fires_on_weekends_and_time_zone_is_ist_not_the_servers():
    assert due_jobs(at(14, 9, 24)) == [] and due_jobs(at(15, 9, 24)) == []                      # Sat, Sun
    assert due_jobs(at(11, 9, 24).astimezone(ZoneInfo("UTC"))) == ["prediction"]                # 03:54 UTC is 09:24 IST
    assert due_jobs(at(11, 3, 54).astimezone(ZoneInfo("UTC"))) != ["prediction"]


def test_scheduler_launches_each_job_once_per_minute_and_skips_a_job_still_running():
    import threading

    launched, release, clock = [], threading.Event(), [at(11, 9, 24)]

    def launch(job):
        launched.append(job)
        release.wait(2)

    sched = Scheduler(".", clock=lambda: clock[0], launch=launch)
    assert sched.tick() == ["prediction"]
    assert sched.tick() == []                                  # same minute: not started again
    clock[0] = at(11, 9, 27)
    assert sched.tick() == ["paper-entry"]
    release.set()
