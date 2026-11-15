from __future__ import annotations

import argparse
import os
import subprocess
import sys

from .runtime import load_environment, child_environment
from .tasks import TASKS, task_module


def main():
    load_environment()
    parser = argparse.ArgumentParser(description="Stockie technical analysis backend")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Serve the API and existing dashboard")
    serve.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.getenv("PORT", "5001")))
    run = commands.add_parser("run", help="Run one existing workflow with its CLI arguments")
    run.add_argument("task", choices=sorted(TASKS))
    run.add_argument("args", nargs=argparse.REMAINDER)
    commands.add_parser("tasks", help="List available workflows")
    commands.add_parser("scheduler", help="Run the daily job roster at its IST times (hosted scheduler)")
    args = parser.parse_args()
    if args.command == "tasks":
        print("\n".join(sorted(TASKS)))
    elif args.command == "scheduler":
        import logging
        from .railway_scheduler import Scheduler, default_workspace

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        Scheduler(default_workspace()).run_forever()
    elif args.command == "run":
        forwarded = args.args[1:] if args.args[:1] == ["--"] else args.args
        raise SystemExit(subprocess.call([sys.executable, "-m", task_module(args.task), *forwarded], env=child_environment()))
    else:
        from waitress import serve as serve_http
        from .app import create_app
        from . import railway_scheduler

        if railway_scheduler.enabled():  # one Railway service: the dashboard/API and the daily job roster
            import logging

            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
            railway_scheduler.Scheduler(railway_scheduler.default_workspace()).start_background()
        serve_http(create_app(), host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
