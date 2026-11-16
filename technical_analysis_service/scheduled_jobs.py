"""Local Windows scheduler entrypoint: explicit environment, windows and logs."""
from __future__ import annotations

import argparse
from datetime import datetime, time
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

IST=ZoneInfo('Asia/Kolkata')
# name, workflow alias/module, arguments, earliest/latest IST, prerequisites
JOBS={
 'us-index':('US Index','market.global',['--mode','us-eur'],'03:15','08:30',[]),
 'kite-token':('Kite Token','@scripts.daily_NIFTY.daily_get_kite_access_token',[],'07:30','08:25',[]),
 'option-instruments':('Option Instruments','options.instruments',['--type','INDEX','--underlying','NIFTY','--underlying','BANKNIFTY'],'08:30','09:10',['kite-token']),
 'macro-refresh':('Macro Refresh','market.macro',[],'09:00','09:18',[]),
 'asia-partial':('Asia Partial','market.global',['--mode','asia-partial'],'09:05','09:14',[]),
 'gift-preopen':('GIFT Preopen','market.gift',['--mode','pre-open'],'09:11','09:14',['kite-token']),
 'option-snapshot':('Option Snapshot Open','options.snapshot',['--underlying','NIFTY','--snapshot-label','OPEN_0915'],'09:15','09:19',['option-instruments']),
 'gift-open':('GIFT Open','market.gift',['--mode','open'],'09:20','09:25',['kite-token']),
 'open-gap':('Open Gap','market.open-gap',[],'09:22','09:30',['gift-open']),
 'prediction':('Prediction and Selection','production.signal',['--underlying','NIFTY','--model-version','cascade_v1'],'09:24','09:40',['us-index','option-snapshot','open-gap','macro-refresh','asia-partial']),
 'paper-entry':('Paper Entry','paper.entry',['--underlying','NIFTY','--model-version','cascade_v1','--max-stale-seconds','300'],'09:27','09:45',['prediction']),
 'morning-email':('Morning Email','@scripts.daily_NIFTY.daily_morning_email',['--underlying','NIFTY','--model-version','cascade_v1'],'09:32','11:00',[]),
 'paper-monitor':('Paper Monitor','paper.monitor',['--underlying','NIFTY','--model-version','cascade_v1','--max-stale-seconds','300','--skip-pending-entry'],'09:30','15:35',[]),
 'gift-close':('GIFT Close','market.gift',['--mode','close'],'15:15','15:20',['kite-token']),
 'paper-report':('Paper Report','paper.report',['--underlying','NIFTY','--model-version','cascade_v1'],'15:32','18:00',[]),
 'asia-final':('Asia Final','market.global',['--mode','asia-final'],'15:45','20:00',[]),
 'market-refresh':('Market Refresh','market.refresh',['--underlying','NIFTY'],'15:47','20:00',['kite-token']),
 'option-ohlc':('Option OHLC Audit','options.ohlc',['--underlying','NIFTY'],'16:00','20:00',['kite-token']),
 'signal-summary':('Signal Summary','@scripts.daily_NIFTY.daily_signal_summary',['--underlying','NIFTY','--model-version','cascade_v1'],'16:10','20:00',[]),
}


def window_status(now: datetime, earliest: str, latest: str) -> str | None:
    local=now.astimezone(IST).time().replace(tzinfo=None)
    if local < time.fromisoformat(earliest): return 'TOO_EARLY'
    if local > time.fromisoformat(latest): return 'SKIPPED_LATE'
    return None


def dependency_failures(jobs, status_dir: Path, today: str) -> list[str]:
    failures=[]
    for job in jobs:
        try: state=json.loads((status_dir/(job+'.json')).read_text(encoding='utf-8'))
        except (OSError,ValueError): state={}
        if state.get('date')!=today or state.get('status')!='SUCCESS': failures.append(job)
    return failures


def redact(text: str, secrets: list[str]) -> str:
    for secret in sorted(secrets,key=len,reverse=True):
        if len(secret)>=6: text=text.replace(secret,'[REDACTED]')
    return re.sub(r'((?:request_token|access_token|api_key)=)[^\s&"<>]+',r'\1[REDACTED]',text,flags=re.I)


def trading_day(today):
    from technical_analysis_service.common.config import get_settings
    from technical_analysis_service.data_manager.db.client_factory import get_database_client
    db=get_database_client(get_settings()); db.connect()
    try:
        with db.conn.cursor() as cursor:
            cursor.execute('SELECT is_trading_day FROM "TradingCalendar" WHERE exchange=%s AND calendar_date=%s',('NSE',today))
            result=cursor.fetchone()
        if result is None: raise RuntimeError('NSE calendar row missing; refusing to assume an open session')
        return bool(result[0])
    finally: db.close()


def run(job: str, workspace: Path, probe=False) -> int:
    workspace=workspace.resolve()
    os.chdir(workspace)
    os.environ.setdefault('TECHNICAL_ANALYSIS_ENV_FILE',str(workspace/'.env'))
    os.environ.setdefault('TECHNICAL_ANALYSIS_OUTPUT_DIR',str(workspace/'output'))
    os.environ['PYTHONUNBUFFERED']='1'
    from technical_analysis_service.runtime import load_environment
    load_environment()
    now=datetime.now(IST); today=now.date().isoformat()
    folder=workspace/'output/scheduler'
    states=folder/('probes' if probe else 'status'); states.mkdir(parents=True,exist_ok=True)
    logs=folder/'logs'/today; logs.mkdir(parents=True,exist_ok=True)
    log=logs/(job+'_'+now.strftime('%H%M%S_%f')+('.probe.log' if probe else '.log'))
    secrets=[value for key,value in os.environ.items() if any(part in key.upper() for part in ('PASSWORD','SECRET','TOKEN','API_KEY','CONN_STR'))]
    state=dict(job=job,date=today,started_at=now.isoformat(),status='STARTING',pid=os.getpid(),log=str(log),probe=probe)
    def finish(status,code,message=''):
        state.update(status=status,exit_code=code,finished_at=datetime.now(IST).isoformat(),message=redact(message,secrets))
        temporary=states/(job+'.'+str(os.getpid())+'.tmp')
        temporary.write_text(json.dumps(state,indent=2)+'\n',encoding='utf-8'); temporary.replace(states/(job+'.json'))
        with log.open('a',encoding='utf-8') as handle: handle.write(json.dumps(state)+'\n')
        print(json.dumps(state),flush=True)
        return code
    if probe:
        # Exercises the scheduler account, cwd, venv and imports; no provider calls.
        from technical_analysis_service.tasks import task_module
        for definition in JOBS.values():
            if not definition[1].startswith('@'): task_module(definition[1])
        return finish('PROBE_SUCCESS',0,'Interpreter: '+sys.executable+'; cwd: '+str(workspace))
    _,workflow,args,earliest,latest,dependencies=JOBS[job]
    timing=window_status(now,earliest,latest)
    if timing: return finish(timing,0 if timing=='SKIPPED_LATE' else 4,'Allowed IST window '+earliest+'–'+latest)
    try:
        if job=='kite-token':
            if os.getenv('KITE_AUTO_LOGIN','1').lower() in {'0','false','no'} or os.getenv('KITE_2FA_MODE','totp').lower()=='manual':
                return finish('FAILED',1,'Unattended token refresh requires automatic login and non-interactive 2FA')
            os.environ['KITE_LOGIN_HEADLESS']='1'
        if job!='us-index' and not trading_day(now.date()): return finish('SKIPPED_HOLIDAY',0,'NSE calendar is closed')
        failed=dependency_failures(dependencies,states,today)
        if failed: return finish('BLOCKED_DEPENDENCY',3,'No same-day success: '+', '.join(failed))
        from technical_analysis_service.tasks import task_module
        module='technical_analysis_service.'+workflow[1:] if workflow.startswith('@') else task_module(workflow)
        state['status']='RUNNING'
        (states/(job+'.json')).write_text(json.dumps(state,indent=2)+'\n',encoding='utf-8')
        command=[sys.executable,'-u','-m',module,*args]
        timeout=210 if job=='paper-monitor' else 1200
        with log.open('a',encoding='utf-8') as handle: handle.write('Started '+now.isoformat()+'\nModule: '+module+'\n')
        child=subprocess.Popen(command,cwd=workspace,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace')
        try: output,_=child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name=='nt': subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],capture_output=True)
            else: child.kill()
            output,_=child.communicate()
            with log.open('a',encoding='utf-8') as handle: handle.write(redact(output,secrets))
            return finish('TIMEOUT',124,'Workflow exceeded '+str(timeout)+' seconds')
        with log.open('a',encoding='utf-8') as handle: handle.write(redact(output,secrets))
        return finish('SUCCESS' if child.returncode==0 else 'FAILED',child.returncode)
    except Exception as exc: return finish('FAILED',1,str(exc))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--job',choices=sorted([*JOBS,'gift-capture']))
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--probe',action='store_true')
    arguments=parser.parse_args()
    if arguments.status:
        states=arguments.workspace/'output/scheduler/status'
        rows=[]
        for key in JOBS:
            try: row=json.loads((states/(key+'.json')).read_text(encoding='utf-8'))
            except (OSError,ValueError): row=dict(job=key,status='NOT_RUN')
            rows.append(row)
        print(json.dumps(rows,indent=2)); raise SystemExit(0)
    if not arguments.job: parser.error('--job is required unless --status is used')
    job=arguments.job
    if job=='gift-capture':
        job='gift-preopen' if datetime.now(IST).time() < time(9,15) else 'gift-open'
    raise SystemExit(run(job,arguments.workspace,arguments.probe))
