"""HTTP boundary for research, production and paper-trading workflows."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
import hmac
import os
import threading

from flask import Blueprint, Flask, jsonify, request

from .jobs import JobBusyError, JobStore
from .tasks import TASKS


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def create_app(*, job_store: JobStore | None = None):
    # Retain existing Research / Production / Trades views in the service.
    from . import dashboard
    app = Flask(__name__)
    for rule in dashboard.app.url_map.iter_rules():
        if rule.endpoint != "static":
            app.add_url_rule(rule.rule, endpoint=rule.endpoint,
                             view_func=dashboard.app.view_functions[rule.endpoint],
                             methods=rule.methods, defaults=rule.defaults)
    app.after_request(dashboard.disable_html_cache)
    api = Blueprint("technical_api", __name__, url_prefix="/api/v1")
    store = job_store
    store_lock = threading.Lock()

    def get_store():
        nonlocal store
        with store_lock:
            if store is None:
                store = JobStore()
            return store

    @app.before_request
    def authenticate_service():
        # Fails closed: with no key configured every route except /health is refused, unless the operator
        # explicitly opts out for trusted local use (TECHNICAL_ANALYSIS_ALLOW_UNAUTHENTICATED=1).
        if request.path == "/health" or _truthy(os.getenv("TECHNICAL_ANALYSIS_ALLOW_UNAUTHENTICATED")):
            return None
        key = os.getenv("TECHNICAL_ANALYSIS_API_KEY", "")
        if not key:
            return jsonify(error="TECHNICAL_ANALYSIS_API_KEY is not configured on the server."), 503
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied, key):
            return jsonify(error="Unauthorized"), 401

    @api.get("/tasks")
    def tasks():
        return jsonify(tasks=list(TASKS))

    @api.get("/strategies")
    def strategies():
        from .analysis.strategy_registry import get_strategy_registry
        registry = get_strategy_registry()
        return jsonify(strategies=[asdict(registry.get_meta(name)) for name in registry.variants])

    @api.post("/jobs")
    def submit():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("task"), str):
            return jsonify(error="Expected JSON with task and optional args list"), 400
        try:
            job = get_store().submit(payload["task"], payload.get("args", []))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except JobBusyError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(job), 202, {"Location": f"/api/v1/jobs/{job['id']}"}

    @api.get("/jobs")
    def jobs():
        return jsonify(jobs=get_store().list())

    @api.get("/jobs/<job_id>")
    def job(job_id):
        found = get_store().get(job_id)
        return (jsonify(found), 200) if found else (jsonify(error="Job not found"), 404)

    @api.get("/production")
    def production():
        from .dashboard import load_production_signal_rows
        try:
            start = date.fromisoformat(request.args.get("start", f"{date.today().year}-01-01"))
            end = date.fromisoformat(request.args.get("end", date.today().isoformat()))
            if end < start:
                raise ValueError("end must be on or after start")
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        rows, error = load_production_signal_rows(start, end)
        return jsonify(rows=rows, error=error), (503 if error else 200)

    @api.get("/paper/trades")
    def paper_trades():
        from .dashboard import load_paper_trade_rows
        try:
            trade_date = date.fromisoformat(request.args.get("trade_date", date.today().isoformat()))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        rows, error = load_paper_trade_rows(trade_date)
        return jsonify(rows=rows, error=error), (503 if error else 200)

    app.register_blueprint(api)
    return app
