from datetime import datetime
import json
from zoneinfo import ZoneInfo

from technical_analysis_service.scheduled_jobs import JOBS, window_status, dependency_failures, redact
from technical_analysis_service.tasks import task_module


def test_all_scheduled_workflows_resolve_and_dependencies_exist():
    for _,workflow,_,earliest,latest,dependencies in JOBS.values():
        if not workflow.startswith('@'): assert task_module(workflow).startswith('technical_analysis_service.')
        assert earliest<=latest
        assert set(dependencies)<=set(JOBS)


def test_morning_entry_cannot_catch_up_in_afternoon():
    now=datetime(2026,11,9,15,57,tzinfo=ZoneInfo('Asia/Kolkata'))
    assert window_status(now,'09:27','09:45')=='SKIPPED_LATE'
    assert window_status(now.replace(hour=9,minute=27),'09:27','09:45') is None
    assert window_status(now.replace(hour=9,minute=26),'09:27','09:45')=='TOO_EARLY'


def test_dependencies_require_same_day_success(tmp_path):
    path=tmp_path/'open-gap.json'
    path.write_text(json.dumps(dict(date='2026-11-08',status='SUCCESS')))
    assert dependency_failures(['open-gap'],tmp_path,'2026-11-09')==['open-gap']
    path.write_text(json.dumps(dict(date='2026-11-09',status='BLOCKED_DEPENDENCY')))
    assert dependency_failures(['open-gap'],tmp_path,'2026-11-09')==['open-gap']
    path.write_text(json.dumps(dict(date='2026-11-09',status='SUCCESS')))
    assert dependency_failures(['open-gap'],tmp_path,'2026-11-09')==[]


def test_scheduler_logs_redact_credentials_and_redirect_tokens():
    safe=redact('password-secret https://example.test?api_key=abc&request_token=def',['password-secret'])
    assert 'password-secret' not in safe and 'abc' not in safe and 'def' not in safe


def test_automatic_token_refresh_does_not_bind_callback_port():
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import Mock
    source=Path(__file__).parents[1]/'scripts/daily_NIFTY/daily_get_kite_access_token.py'
    tree=ast.parse(source.read_text(encoding='utf-8'))
    function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='get_kite_client')
    callback=Mock()
    namespace=dict(load_token=lambda:None,sys=SimpleNamespace(argv=['token.py']),kite=Mock(),
        AUTO_LOGIN=True,latest_access_token='test-only-token',start_callback_server=callback,
        automate_kite_login=Mock(),token_event=SimpleNamespace(wait=lambda timeout:True),TOKEN_WAIT_SECONDS=1)
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(source),'exec'),namespace)
    namespace['get_kite_client']()
    callback.assert_not_called()
    namespace['automate_kite_login'].assert_called_once()
