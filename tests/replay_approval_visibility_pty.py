"""Synthetic production approvals in a kernel PTY; never execute historical scripts.

The parent launches the existing strict offline guard. The child uses the real
review payload, human input transport, parser and harmless executor. Events are
harness synchronization only; they neither approve nor alter production state.
"""
import argparse
import asyncio
import codecs
import concurrent.futures
from copy import deepcopy
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from unittest.mock import patch

from tests.replay_cli_v2_pty import VTTextScreen, atomic_json, pulse, PYTHON, DEPS

PROMPT = '[y/Enter=run, n=reject, q=quit, e:=edit] > '


def fixture_code():
    return '\n'.join(['# Synthetic approved candidate; no scientific input'] +
                     [f'# body-line-{i:03d} synthetic review context' for i in range(430)] +
                     ["printf 'approval-fixture-executed\\n'"])






def record(source, root, *, scenario='approve', mode='tty', verbosity='compact', cols=80, rows=24,
           old_race=False, resize=None, installed_cli=False):
    source=Path(source).resolve();root=Path(root).resolve()
    assert source.is_relative_to(Path('/tmp')) and root.is_relative_to(Path('/tmp'))
    manifest=json.loads((source/'export_manifest.json').read_text())
    assert all(hashlib.sha256((source/n).read_bytes()).hexdigest()==d for n,d in manifest['files'].items())
    root.mkdir(parents=True,exist_ok=False);(root/'scratch').mkdir()
    env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8','PYTHONPATH':'' if installed_cli else DEPS,'TERM':'xterm-256color',
         'COLUMNS':str(cols),'LINES':str(rows),'PYTHONDONTWRITEBYTECODE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1',
         'TMPDIR':str(root/'scratch'),'BIOMIND_WIKI_ENABLED':'0','LOG_LEVEL':'CRITICAL',
         'BIOMIND_OFFLINE_RESULTS':str(root/'pytest-results.json'),'BIOMIND_CLI_PROGRESS_ARTIFACTS':str(root),
         'BIOMIND_GER_ARTIFACTS':str(root),'BIOMIND_EXEC_LOG_DIR':str(root/'scratch/executor-logs'),
         'BIOMIND_APPROVAL_PTY':'1','BIOMIND_APPROVAL_SCENARIO':scenario,'BIOMIND_UI':mode,
         'BIOMIND_CLI_VERBOSITY':verbosity,'BIOMIND_APPROVAL_DELAY_WRITER':'1' if old_race else '0'}
    command=[PYTHON,str(source/'tests/cli_model_progress_support.py'),'--guarded-child',str(source),
             '-q','-s','tests/replay_approval_visibility_pty.py::test_production_approval_pty_child']
    master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',rows,cols,0,0))
    def attach_terminal():
        os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
    child=subprocess.Popen(command,cwd=source,env=env,stdin=slave,stdout=slave,stderr=slave,preexec_fn=attach_terminal)
    os.close(slave)
    screen=VTTextScreen(cols,rows); decoder=codecs.getincrementaldecoder('utf-8')('replace')
    raw=bytearray();actions=[];frames={};seen=set();started=time.monotonic();quiet=time.monotonic();last_index=-1
    def read(name):
        try:return json.loads((root/name).read_text())
        except (OSError,ValueError):return {}
    def send(value,label):
        os.write(master,value);actions.append({'at':time.monotonic(),'action':label})
    def snap(name):
        frames[name]={'at':time.monotonic(),'screen':screen.text(),'state':read('frame-state.json'),
                      'executions':len(read('business.json').get('executions',[]))}
        (root/(name+'.screen.txt')).write_text(screen.text())
    try:
        while time.monotonic()-started < 55:
            if select.select([master],[],[],.015)[0]:
                try:data=os.read(master,65536)
                except OSError as exc:
                    if exc.errno==errno.EIO:break
                    raise
                if not data:break
                raw.extend(data);quiet=time.monotonic()
                for reply in screen.feed(decoder.decode(data)):send(reply.encode(),'cursor_report')
            phase=read('phase.json');state=read('frame-state.json');text=raw.decode('utf-8','replace')
            index=phase.get('index',0)
            visible=(state.get('pending') and state.get('frames',0)>0) if mode=='tty' else ((PROMPT if phase.get('stage')!='plan_review' else 'Approve? [y/n/q]') in text)
            if old_race and (root/'writer-blocked.json').exists() and 'released' not in seen:
                # Old queue writer is blocked while foreground input writes
                # prompt; new implementation is allowed to show no prompt yet.
                if PROMPT in text or 'Approve? [y/n/q]' in text or time.monotonic()-quiet>.5:
                    snap('before-body-release');(root/'release_writer').write_text('release finite writer batch');seen.add('released')
            if phase.get('phase')=='review_requested' and visible and index!=last_index:
                last_index=index; snap('pending-'+str(index))
                if mode!='none':(root/'flood').write_text('send bounded background batch')
                else:(root/'flood-done.json').write_text('{"events":0}')
                seen.add('pending-'+str(index))
            if (root/'flood-done.json').exists() and visible and phase.get('phase')=='review_requested':
                # Settling is not the race trigger: finite producer completion
                # and render generation/prompt writes establish readiness.
                if time.monotonic()-quiet < .15:continue
                if 'flood-snapshot' not in seen:
                    snap('after-background');seen.add('flood-snapshot')
                    if resize:
                        c,r=resize;fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',r,c,0,0));screen.resize(c,r)
                        os.kill(child.pid,signal.SIGWINCH);seen.add('resize');continue
                if resize and 'resized-snapshot' not in seen:
                    snap('after-resize');seen.add('resized-snapshot')
                if scenario=='details' and mode=='tty':
                    number=next((i for i in range(3) if f'detail-closed-{i}' not in seen),None)
                    if number is not None:
                        opened=f'detail-open-{number}';entered=f'detail-enter-{number}';closing=f'detail-closing-{number}'
                        if opened not in seen:
                            if state.get('modal'):continue
                            key=(b'/details\r',b'\x1bOQ',b'\x0f')[number]
                            send((b'draft-feedback' if number==1 else b'')+key,f'details-open-{number}')
                            seen.add(opened);continue
                        if entered not in seen:
                            if not state.get('modal'):continue
                            snap('details-'+str(number));target_submit=read('submit-events.json').get('count',0)+1
                            send(b'\r','enter_inside_details');seen.add(entered);continue
                        if closing not in seen:
                            submit=read('submit-events.json')
                            if submit.get('count',0)<target_submit:continue
                            assert submit['last']['before']['modal'],submit
                            assert submit['last']['before']['pending_identity']==submit['last']['after_pending_identity'],submit
                            send(b'\x1b','escape_details');seen.add(closing);continue
                        if state.get('modal'):continue
                        if state.get('frames',0)<=frames['details-'+str(number)]['state']['frames']:continue
                        seen.add(f'detail-closed-{number}');continue
                    if 'details-draft-preserved' not in seen:
                        snap('details-returned');assert state.get('draft')=='draft-feedback',state
                        send(b'\x15','clear_local_draft');seen.add('details-draft-preserved');continue
                    if state.get('draft'):continue
                if scenario=='edit' and index==0 and 'edited' not in seen:
                    send(b'e: printf edited-fixture-executed\r','edit_candidate');seen.add('edited');continue
                if scenario=='edit' and index==0:continue
                if f'decision-{index}' not in seen:
                    snap('before-decision-'+str(index))
                    response={'reject':b'n\r','feedback':b'please keep the original scope\r','quit':b'q\r',
                              'eof':b'\x04','cancel':b'\x03','y':b'y\r',
                              'paste':b'\x1b[200~feedback first\n/details\nfeedback last\x1b[201~\r'}.get(scenario,b'\r')
                    if phase.get('stage')=='plan_review':response=b'y\r'
                    send(response,'decision_'+scenario);seen.add(f'decision-{index}')
            if phase.get('phase')=='business_done' and 'completed' not in seen:
                snap('completed');(root/'finish').write_text('capture complete');seen.add('completed')
            if child.poll() is not None:break
        if child.poll() is None:child.terminate()
        returncode=child.wait(timeout=5)
    finally:
        os.close(master)
        if child.poll() is None:child.kill();child.wait()
    (root/'terminal.raw').write_bytes(raw)
    # Preserve an unchanged bounded ANSI segment of the real PTY. Bootstrap
    # and test-source inventories remain in terminal.raw for provenance.
    begin=raw.find(b'\x1b[2J\x1b[H');end=raw.find(b'PYTEST_EXIT_CODE=')
    (root/'terminal.ansi').write_bytes(raw[max(0,begin):end if end>=0 else None])
    unchanged=all(hashlib.sha256((source/n).read_bytes()).hexdigest()==d for n,d in manifest['files'].items())
    result={'returncode':returncode,'actual_kernel_pty':True,'production_input':True,'scenario':scenario,
            'installed_cli':installed_cli,'mode':mode,'verbosity':verbosity,'source':manifest['source'],'source_signature':manifest['source_signature'],
            'source_unchanged':unchanged,'initial_size':[cols,rows],'resize':resize,'old_writer_barrier':old_race,
            'actions':actions,'frames':frames,'business':read('business.json'),'error':read('child-error.json'),
            'unsupported_vt':sorted(set(screen.unknown)), 'complete':'completed' in seen,
            'stale_resolution':read('stale-resolution.json')}
    atomic_json(root/'result.json',result);atomic_json(root/'invocation.json',{'command':command,'environment':env})
    print(json.dumps({k:v for k,v in result.items() if k not in {'actions','frames','business'}},ensure_ascii=False))
    return result


def process_lifecycle_is_complete(rows):
    """One real process lifetime may contain scheduler-dependent heartbeats."""
    args = [row.get('args', []) for row in rows]
    if len(args) < 2 or any(len(row) != 2 or not isinstance(row[1], dict) for row in args):
        return False
    if args[0][0] != 'RUNNING' or args[-1][0] != 'PROCESS_EXITED':
        return False
    if any(row[0] != 'HEARTBEAT' for row in args[1:-1]):
        return False
    pid, pgid = args[0][1].get('pid'), args[0][1].get('pgid')
    return (pid is not None and pgid is not None
            and all(row[1].get('pid') == pid and row[1].get('pgid') == pgid for row in args)
            and args[-1][1].get('returncode') == 0)


def validate_result(result, *, fixed=True):
    """Machine assertions use the last screen and real receipts, not prompt logs."""
    b=result['business']; scenario=result['scenario']; mode=result['mode']; frames=result['frames']
    checks={
        'child_completed':result['returncode']==0 and result['complete'],
        'source_unchanged':result['source_unchanged'],
        'kernel_pty_and_production_input':result['actual_kernel_pty'] and result['production_input'],
        'vt_decoder_has_no_unknown_operations':not result['unsupported_vt'],
        'no_unexpected_model_calls':b['model_calls']==(2 if scenario in {'plan','sequential'} else 0),
    }
    expected=0 if scenario in {'plan','reject','feedback','quit','eof','cancel','paste'} else 1
    checks['execution_count']=len(b['executions'])==expected
    before=[v for n,v in frames.items() if n.startswith('before-decision-')]
    if scenario!='auto':
        checks['undecided_execution_zero']=bool(before) and all(v['executions']==0 for v in before)
        current=frames.get('after-resize',frames.get('after-background',{})).get('screen','')
        visible=('> ' in current or '>\n' in current or current.rstrip().endswith('>')) and ('Enter' in current or '[y/n/q]' in current)
        checks['last_screen_prompt_visible' if fixed else 'old_last_screen_prompt_lost']=visible if fixed else not visible
        if fixed and mode!='none':checks['waiting_state_visible']='等待' in current and '审批' in current
    if expected:
        execution=b['executions'][0]
        expected_code=("echo 'approval-fixture-executed'" if scenario=='auto' else
                       'printf edited-fixture-executed' if scenario=='edit' else fixture_code())
        checks['executed_original_approved_bytes']=execution['script_sha256']==hashlib.sha256(expected_code.encode()).hexdigest()
        checks['real_process_succeeded']=execution['returncode']==0 and execution['stdout']==(
            'edited-fixture-executed' if scenario=='edit' else 'approval-fixture-executed\n')
        checks['actual_spawn_and_exit_receipts']=process_lifecycle_is_complete(execution['lifecycle'])
    if scenario in {'plan','sequential'}:
        checks['actual_fifteen_step_plan_review']=b.get('plan_review_outcome',{}).get('type')=='approved_for_execution' and b['requests'][0]['steps']==15
    if scenario=='sequential':
        checks['two_distinct_approval_requests']=len(b['requests'])==2 and len(before)==2
    if scenario=='details':
        views=[frames.get('details-'+str(i),{}) for i in range(3)]
        ids=[view.get('state',{}).get('pending_identity') for view in views]
        checks['details_keep_same_unresolved_future']=all(i is not None for i in ids) and len(set(ids))==1 and all(view.get('executions')==0 for view in views)
        checks['details_show_actual_body']='body-line-000' in views[0].get('screen','')
        checks['details_keep_draft']=frames.get('details-returned',{}).get('state',{}).get('draft')=='draft-feedback'
    if scenario=='edit':
        checks['edited_body_gets_fresh_approval']=len(b['requests'])==2 and b['requests'][0]['sha256']!=b['requests'][1]['sha256']
        checks['stale_callback_cannot_complete_new_request']=result.get('stale_resolution')=={'new_future_unchanged':True,'new_future_done':False}
    if scenario=='paste':
        checks['paste_is_one_feedback_decision']=len(b['decisions'])==1 and b['decisions'][0]['action']=='reject' and '/details' in b['decisions'][0]['selected']
    if scenario=='eof':checks['eof_not_empty_approval']=b.get('input_exception')=='HumanInputEOF'
    if fixed and mode in {'tty','plain','off'} and scenario not in {'auto','eof','cancel'}:
        events=b.get('approval_events',[])
        checks['production_monotonic_lifecycle']=isinstance(events,list) and bool(events)
        if isinstance(events,list):
            for index in sorted({row['request_id'] for row in events}):
                rows=[row for row in events if row['request_id']==index]
                phases={row['phase']:row['monotonic'] for row in rows}
                written=phases.get('frame_rendered',phases.get('presentation_written'))
                checks['lifecycle_order_'+str(index)]=all(name in phases for name in ('request_created','presentation_started','input_ready','decision_received')) and written is not None and phases['request_created']<=phases['presentation_started']<=written<=phases['input_ready']<=phases['decision_received']
    return {'passed':all(checks.values()),'checks':checks}


def main():
    p=argparse.ArgumentParser();p.add_argument('--export',type=Path);p.add_argument('--results',type=Path)
    p.add_argument('--validate-result',type=Path);p.add_argument('--assert-fixed',action='store_true')
    p.add_argument('--mode',default='tty',choices=['tty','plain','off','none']);p.add_argument('--scenario',default='approve')
    p.add_argument('--installed-cli',action='store_true')
    p.add_argument('--verbosity',default='compact');p.add_argument('--old-race',action='store_true')
    p.add_argument('--cols',type=int,default=80);p.add_argument('--rows',type=int,default=24);p.add_argument('--resize')
    a=p.parse_args()
    if a.validate_result:
        value=validate_result(json.loads(a.validate_result.read_text()),fixed=a.assert_fixed)
        print(json.dumps(value,ensure_ascii=False));raise SystemExit(0 if value['passed'] else 1)
    if a.export is None or a.results is None:p.error('--export and --results are required for replay')
    value=record(a.export,a.results,scenario=a.scenario,mode=a.mode,verbosity=a.verbosity,
        installed_cli=a.installed_cli,old_race=a.old_race,cols=a.cols,rows=a.rows,resize=tuple(map(int,a.resize.split('x'))) if a.resize else None)
    if a.assert_fixed:
        assertions=validate_result(value,fixed=True);atomic_json(a.results/'assertions.json',assertions)
        if not assertions['passed']:print(json.dumps(assertions,ensure_ascii=False));raise SystemExit(1)
    raise SystemExit(value['returncode'] or (0 if value['complete'] else 2))
if __name__=='__main__':main()
