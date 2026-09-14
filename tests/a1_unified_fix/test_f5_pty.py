"""F5 real kernel PTY, production Plan graph/review/input; fixed provider only.

The executable parent imports only safe test support. The child loads production
through the unchanged allowlisted export guard and owns new synthetic paths.
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

# Parent needs no BioMind import or ambient configuration.
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tests.replay_cli_v2_pty import VTTextScreen, atomic_json, pulse, PYTHON


async def production_plan(owner, root, scenario):
    import bio_agents
    from langchain_core.messages import AIMessage
    from agent_core.plan_tracker import load_plan_state, save_plan_state
    from agent_core.planner_loop import plan_review_submission_readiness, plan_review_acceptance_readiness
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.ger_fixture_support import configure_protocol
    case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    await case.asyncSetUp();configure_protocol(case);case._configure_one_step_no_output_plan()
    task='f5-plan-current';router=bio_agents.OutputRouter()
    records={'scenario':scenario,'provider_calls':[],'reviews':[],'updates':[],'executions':0,'code_approvals':0}
    # Remove the old fixture's auto-code-approval substitute. A real Plan
    # approval must leave this separate, real middleware entry untouched.
    del case.agent.middleware.review_code
    review_code=case.agent.middleware.review_code
    def observed_code_review(*args,**kwargs):
        records['code_approvals'] += 1
        return review_code(*args,**kwargs)
    case.agent.middleware.review_code=observed_code_review
    def persist():atomic_json(root/'business.json',records)
    plan=case._planner_payload();first=deepcopy(plan['steps'][0])
    plan['steps']=[dict(deepcopy(first),step_id=f'plan-inspect-{i}',title=f'合成步骤 {i:02d} / synthetic stage with spaces',
        dependencies=[] if i==0 else [f'plan-inspect-{i-1}'],
        success_criteria=[f'冻结要求 {i:02d}-{j:02d}: inspect synthetic facts; retain unknown evidence and stop before unauthorized execution' for j in range(12)],
        produces_observations=[f'fixture_observation_{i:02d}']) for i in range(12)]
    async def provider(messages,*,stage,**kwargs):
        records['provider_calls'].append(stage)
        if stage=='consultant':value=case._consultant_payload(case.root/'中文 空格'/'unused report.md')
        elif stage.startswith('planner'):value=plan
        else:raise AssertionError('No Generate/provider call permitted after Plan review: '+stage)
        return AIMessage(content=json.dumps(value,ensure_ascii=False),response_metadata={'finish_reason':'stop'})
    case.agent._ainvoke_node_llm=provider;case.agent._ainvoke_llm=provider
    case.agent.input_timeout_seconds=0
    original_prompt=bio_agents.BioAnalystAgent._prompt_user.__get__(case.agent)
    def prompt(task_id,stage,text,payload=None,**kwargs):
        assert stage in {'plan_review','plan_review_feedback'}
        if stage=='plan_review':
            view=router.review_display;durable=load_plan_state(case.root,task_id)
            records['reviews'].append({'body':view.body,'body_sha256':hashlib.sha256(view.body.encode()).hexdigest(),
                'kind':view.kind,'submission':plan_review_submission_readiness(durable),'plan_id':durable.get('plan_id'),
                'plan_version':durable.get('plan_version'),'payload':deepcopy(payload)})
            persist();atomic_json(root/'phase.json',{'phase':'review_requested'})
            if scenario in {'semantic','nonsemantic'}:
                def mutate():
                    while not (root/'mutate').exists():time.sleep(.005)
                    if scenario=='semantic':
                        case.agent._persist_user_submission(task_id,'A new authoritative synthetic requirement replaces the displayed request.')
                    else:
                        current=load_plan_state(case.root,task_id)
                        current.setdefault('task_state',{})['synthetic_observer_note']='presentation-only observer update'
                        save_plan_state(case.root,task_id,current)
                    atomic_json(root/'mutation.json',{'scenario':scenario,'readiness':plan_review_submission_readiness(load_plan_state(case.root,task_id))})
                threading.Thread(target=mutate,daemon=True).start()
        else:
            atomic_json(root/'phase.json',{'phase':'feedback_requested'})
        response=original_prompt(task_id,stage,text,payload=payload,**kwargs)
        records.setdefault('input',[]).append({'stage':stage,'response':response});persist()
        return response
    case.agent._prompt_user=prompt
    try:
        with patch.object(bio_agents,'output',router):
            runner=case._runner_state(task);app=case.agent._app_for_task(task,runner)
            stream=app.astream(runner,config={'configurable':{'thread_id':task},'recursion_limit':15},stream_mode='updates')
            try:
                async for update in stream:
                    records['updates'].append(deepcopy(update))
                    if 'plan_review' in update:
                        records['outcome']=deepcopy(update['plan_review'].get('plan_review_outcome',{}));break
            finally:await stream.aclose()
        durable=load_plan_state(case.root,task)
        records['acceptance']=plan_review_acceptance_readiness(durable)
        records['approval_scope']=durable.get('approval_scope')
        records['current_approval']=durable.get('current_approval')
        records['durable_status']=durable.get('task_state',{}).get('status')
        records['question_waiting']=durable.get('task_state',{}).get('waiting_state')
    except (KeyboardInterrupt,asyncio.CancelledError) as exc:
        records['control_exception']=type(exc).__name__
    finally:
        durable=load_plan_state(case.root,task)
        records['executions']=len((durable.get('provenance_state') or {}).get('attempts') or {})
        atomic_json(root/'durable.json',durable)
        records['approval_events']=deepcopy(list(getattr(owner,'approval_events',[])))
        if getattr(owner,'plain_owner',None):records['plain_events']=deepcopy(list(owner.plain_owner.approval_events))
        records['input_history']=list(owner.model.history)
        loaded={name:{'path':module.__file__,'sha256':hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
            for name,module in tuple(sys.modules.items()) if getattr(module,'__file__',None) and
            (name=='bio_agents' or name.startswith(('agent_core.','execution.','memory.'))) and str(module.__file__).endswith('.py')}
        atomic_json(root/'loaded.json',loaded)
        persist();atomic_json(root/'phase.json',{'phase':'business_done'})
        await case.asyncTearDown()


def test_f5_guarded_pty_child():
    import pytest
    if os.environ.get('BIOMIND_F5_PTY')!='1':pytest.skip('explicit guarded PTY child only')
    sys.stdin=sys.__stdin__;sys.stdout=sys.__stdout__;sys.stderr=sys.__stderr__
    assert sys.stdin.isatty() and sys.stdout.isatty()
    root=Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS']);mode=os.environ['BIOMIND_UI'];scenario=os.environ['BIOMIND_F5_SCENARIO']
    from agent_core.cli_terminal import Terminal, PlainTerminal
    from agent_core.cli_display import bind_terminal
    if scenario == 'fallback':
        import agent_core.cli_terminal as module
        from agent_core.cli_display import terminal
        owners=[];requests=[];transfers=[]
        original_init=Terminal.__init__;original_read=PlainTerminal.read
        def initialize(owner,**kwargs):
            original_init(owner,**kwargs);owners.append(owner)
            def fail_once(app):
                if owner.pending is not None and owner.input_ready and not requests:
                    requests.append(owner.pending)
                    raise RuntimeError('synthetic F5 renderer callback fault')
            owner.app.after_render += fail_once
        def plain_read(owner,*args,**kwargs):
            transfers.append({'same_future':owners[0].pending is requests[0],
                'body_sha256':hashlib.sha256(owner.review.body.encode()).hexdigest(),
                'kind':owner.review.kind})
            atomic_json(root/'transfer.json',transfers)
            return original_read(owner,*args,**kwargs)
        async def runtime():
            await production_plan(terminal(),root,scenario)
            while not (root/'finish').exists():await asyncio.sleep(.005)
        async def entry():
            pulse(asyncio.get_running_loop())
            async def monitor():
                while True:
                    if owners:
                        owner=owners[0];plain=owner.plain_owner
                        pending=plain.pending if plain else None
                        atomic_json(root/'frame.json',{'frames':owner.frames,'pending':pending is not None,
                            'input_ready':bool(plain and any(e['phase']=='input_ready' for e in plain.approval_events)),
                            'modal':False,'plain':plain is not None,'body_sha256':hashlib.sha256(owner.review_document.encode()).hexdigest(),
                            'view_text':owner.review_document,'cursor':0,'size':list(os.get_terminal_size(1))})
                    await asyncio.sleep(.005)
            observer=asyncio.create_task(monitor())
            try:await module.run_terminal(runtime)
            finally:observer.cancel()
        with patch.object(Terminal,'__init__',initialize),patch.object(PlainTerminal,'read',plain_read):
            asyncio.run(entry())
        return
    async def run():
        owner=Terminal() if mode=='tty' else PlainTerminal()
        previous=bind_terminal(owner);done=concurrent.futures.Future()
        if mode=='tty' and scenario=='queued':
            original=owner._present_input
            async def barrier(request):
                atomic_json(root/'presentation-blocked.json',{'pending':True})
                while not (root/'release-presentation').exists():await asyncio.sleep(.005)
                await original(request)
            owner._present_input=barrier
        def worker():
            try:asyncio.run(production_plan(owner,root,scenario))
            except BaseException as exc:
                atomic_json(root/'child-error.json',{'type':type(exc).__name__,'message':str(exc)})
                done.set_exception(exc)
            else:done.set_result(None)
        def frame():
            view=getattr(owner,'review_document','')
            pending=getattr(owner,'pending',None)
            if mode=='plain' and pending:view=pending['review'].body
            atomic_json(root/'frame.json',{'frames':getattr(owner,'frames',0),'pending':pending is not None,
                'pending_id':id(pending) if pending is not None else None,'input_ready':getattr(owner,'input_ready',True),
                'modal':getattr(owner,'modal',False),'body_sha256':hashlib.sha256(view.encode()).hexdigest(),
                'view_text':getattr(getattr(owner,'details_view',None),'text',''),
                'cursor':getattr(getattr(getattr(owner,'details_view',None),'buffer',None),'cursor_position',0),
                'draft':getattr(getattr(owner,'buffer',None),'text',''),'size':list(os.get_terminal_size(1))})
        if mode=='tty':
            original_render=owner._rendered
            def rendered():original_render();frame()
            owner._rendered=rendered
        async def monitor():
            while not done.done():
                if mode!='tty':frame()
                await asyncio.sleep(.005)
            await asyncio.wrap_future(done)
            while not (root/'finish').exists():await asyncio.sleep(.005)
            if mode=='tty':owner.app.exit()
        pulse(asyncio.get_running_loop());tasks=[]
        def start():
            threading.Thread(target=worker,daemon=True).start();tasks.append(asyncio.create_task(monitor()))
        try:
            if mode=='tty':await owner.app.run_async(pre_run=start,set_exception_handler=False);await tasks[0]
            else:start();await tasks[0]
        finally:
            owner.closed=True;bind_terminal(previous)
    asyncio.run(run())


def record(export,root,mode,scenario):
    export=Path(export).resolve();root=Path(root).resolve()
    assert export.is_relative_to(Path('/tmp')) and root.is_relative_to(Path('/tmp'))
    manifest=json.loads((export/'export_manifest.json').read_text())
    assert all(hashlib.sha256((export/n).read_bytes()).hexdigest()==d for n,d in manifest['files'].items())
    root.mkdir(parents=True,exist_ok=False);(root/'scratch').mkdir()
    env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8','PYTHONPATH':'','TERM':'xterm-256color','COLUMNS':'88','LINES':'26',
        'PYTHONDONTWRITEBYTECODE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1','TMPDIR':str(root/'scratch'),
        'BIOMIND_WIKI_ENABLED':'0','LOG_LEVEL':'CRITICAL','BIOMIND_OFFLINE_RESULTS':str(root/'pytest-results.json'),
        'BIOMIND_CLI_PROGRESS_ARTIFACTS':str(root),'BIOMIND_GER_ARTIFACTS':str(root),
        'BIOMIND_EXEC_LOG_DIR':str(root/'scratch/executor-logs'),'BIOMIND_F5_PTY':'1','BIOMIND_UI':mode,'BIOMIND_F5_SCENARIO':scenario}
    command=[PYTHON,str(export/'tests/cli_model_progress_support.py'),'--guarded-child',str(export),'-q','-s','tests/a1_unified_fix/test_f5_pty.py::test_f5_guarded_pty_child']
    master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',26,88,0,0))
    def attach():os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
    child=subprocess.Popen(command,cwd=export,env=env,stdin=slave,stdout=slave,stderr=slave,preexec_fn=attach);os.close(slave)
    screen=VTTextScreen(88,26);decoder=codecs.getincrementaldecoder('utf-8')('replace');raw=bytearray();actions=[];frames={};seen=set();started=time.monotonic();quiet=started
    def read(name):
        try:return json.loads((root/name).read_text())
        except (OSError,ValueError):return {}
    def send(body,label):os.write(master,body);actions.append({'at':time.monotonic(),'event':label})
    def snapshot(label):
        frames[label]={'at':time.monotonic(),'screen':screen.text(),'state':read('frame.json'),'business':read('business.json')}
        (root/(label+'.screen.txt')).write_text(screen.text())
    try:
        while time.monotonic()-started<70:
            if select.select([master],[],[],.01)[0]:
                try:data=os.read(master,65536)
                except OSError as exc:
                    if exc.errno==errno.EIO:break
                    raise
                if not data:break
                raw.extend(data);quiet=time.monotonic()
                for reply in screen.feed(decoder.decode(data)):send(reply.encode(),'cursor-report')
            state=read('frame.json');phase=read('phase.json').get('phase');text=raw.decode('utf-8','replace')
            ready=state.get('pending') and (state.get('input_ready') if mode=='tty' else 'Approve? [y/n/q]' in text)
            if scenario=='queued' and (root/'presentation-blocked.json').exists() and 'prequeued' not in seen:
                send(b'\r','prequeued-before-ready');seen.add('prequeued');actions.append({'at':time.monotonic(),'event':'hold-unready'});continue
            if scenario=='queued' and 'prequeued' in seen and 'released' not in seen:
                if time.monotonic()-actions[-1]['at']<.15:continue
                assert not read('business.json').get('input');(root/'release-presentation').write_text('release');seen.add('released')
            if phase=='review_requested' and ready and time.monotonic()-quiet>.12:
                if 'default' not in seen:snapshot('default');seen.add('default')
                if scenario in {'semantic','nonsemantic'} and 'mutated' not in seen:
                    (root/'mutate').write_text('new isolated event')
                    if not (root/'mutation.json').exists():continue
                    snapshot('mutated');seen.add('mutated')
                if scenario=='approve' and mode=='tty':
                    if 'scroll' not in seen:send(b'\x1b[6~','scroll-default-plan');seen.add('scroll');continue
                    if 'scrolled' not in seen:snapshot('scrolled');seen.add('scrolled');send(b'\x1bOQ','F2');continue
                    if 'details' not in seen:
                        if not state.get('modal'):continue
                        snapshot('details');send(b'\r','Enter-details-no-approval');seen.add('details');continue
                    if 'escaped' not in seen:
                        assert not read('business.json').get('input');send(b'\x1b','Escape-details');seen.add('escaped');continue
                    if state.get('modal'):continue
                    if 'resize' not in seen:
                        fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',32,67,0,0));screen.resize(67,32);os.kill(child.pid,signal.SIGWINCH);seen.add('resize');continue
                    if 'resized' not in seen:snapshot('resized');seen.add('resized')
                if scenario=='approve' and mode=='plain' and 'details' not in seen:
                    send(b'/details\r','plain-details');seen.add('details');continue
                if scenario=='blank_paste' and 'blank_paste' not in seen:
                    send(b'\x1b[200~\n\x1b[201~\r','blank-paste-not-confirmation');seen.add('blank_paste');continue
                if 'decision' not in seen:
                    if scenario=='blank_paste':assert not read('business.json').get('input')
                    snapshot('before-decision')
                    value={'feedback':'请保留当前输入，说明未决条件。\n/details\n这是同一反馈'.encode(),'reject':b'n','eof':b'\x04','cancel':b'\x03'}.get(scenario,b'')
                    if scenario=='feedback':value=b'\x1b[200~'+value+b'\x1b[201~\r'
                    elif scenario=='reject':value=b'n\r'
                    elif scenario not in {'eof','cancel'}:value+=b'\r'
                    send(value,'explicit-decision');seen.add('decision')
            if phase=='business_done' and 'done' not in seen:snapshot('done');(root/'finish').write_text('captured');seen.add('done')
            # The prompt callback publishes its phase before the next input
            # owner has rendered. Do not reuse the preceding approval frame's
            # readiness to inject a confirmation for the feedback request.
            feedback_pending=state.get('pending_id')
            previous_pending=frames.get('before-decision',{}).get('state',{}).get('pending_id')
            if (phase=='feedback_requested' and ready and feedback_pending is not None
                    and feedback_pending!=previous_pending and time.monotonic()-quiet>.12
                    and 'feedback_sent' not in seen):
                send('请按当前材料重新咨询，保留未决条件。\r'.encode(),'reject-followup-feedback');seen.add('feedback_sent')
            if child.poll() is not None:break
        if child.poll() is None:child.terminate()
        rc=child.wait(timeout=5)
    finally:
        os.close(master)
        if child.poll() is None:child.kill();child.wait()
    (root/'terminal.raw').write_bytes(raw)
    result={'root':str(root),'returncode':rc,'actual_kernel_pty':True,'mode':mode,'scenario':scenario,'source_signature':manifest['source_signature'],
        'parent_harness_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'guarded_child_harness_sha256':hashlib.sha256((export/'tests/a1_unified_fix/test_f5_pty.py').read_bytes()).hexdigest(),
        'source_unchanged':all(hashlib.sha256((export/n).read_bytes()).hexdigest()==d for n,d in manifest['files'].items()),
        'frames':frames,'actions':actions,'business':read('business.json'),'error':read('child-error.json'),'unknown_vt':screen.unknown,
        'transfer':read('transfer.json')}
    atomic_json(root/'invocation.json',{'command':command,'environment':env});atomic_json(root/'result.json',result)
    checks=validate(result);atomic_json(root/'checks.json',checks)
    print(json.dumps({'root':str(root),'returncode':rc,'checks':checks},ensure_ascii=False),flush=True)
    return checks


def validate(result):
    b=result['business'];frames=result['frames'];mode=result['mode'];scenario=result['scenario'];reviews=b.get('reviews',[])
    checks={'child_rc0':result['returncode']==0,'business_done':'done' in frames,'source_unchanged':result['source_unchanged'],
        'one_frozen_plan':len(reviews)==1 and reviews[0].get('kind')=='plan','no_execution_or_code_approval':b.get('executions')==b.get('code_approvals')==0,
        'fixed_provider_only':b.get('provider_calls')==['consultant','planner']}
    if reviews and 'default' in frames:
        body=reviews[0]['body'];shown=frames['default']
        checks['full_body_bound_before_confirmation']=shown['state']['body_sha256']==reviews[0]['body_sha256']
        if mode=='tty':checks['entire_frozen_body_in_current_view']=shown['state']['view_text']==body
        checks['default_body_visible']=any(line.strip() and len(line)>8 and line.strip() in shown['screen'] for line in body.splitlines()[:15]) if mode=='tty' and scenario!='fallback' else body.replace('\n','\r\n') in (Path(result['root'])/'terminal.raw').read_bytes().decode(errors='replace')
        if scenario=='fallback':
            checks['same_pending_future_transferred_once']=len(result['transfer'])==1 and result['transfer'][0]['same_future']
            checks['same_frozen_body_in_plain']=result['transfer'][0]['body_sha256']==reviews[0]['body_sha256'] and result['transfer'][0]['kind']=='plan'
        if scenario=='approve' and mode=='tty':
            checks['same_body_in_f2']=frames.get('details',{}).get('state',{}).get('body_sha256')==reviews[0]['body_sha256']
            # The existing F2 viewer also contains title, prompt and diagnostic
            # sections. The whole frozen body must be present byte-for-byte.
            checks['entire_frozen_body_in_f2']=body in frames.get('details',{}).get('state',{}).get('view_text','')
            checks['default_scrolled']=frames.get('scrolled',{}).get('state',{}).get('cursor',0)>shown['state'].get('cursor',0)
            checks['resize_same_scope']=frames.get('resized',{}).get('state',{}).get('body_sha256')==reviews[0]['body_sha256']
    approved=scenario not in {'semantic','feedback','reject','eof','cancel'}
    checks['correct_acceptance']=bool(b.get('acceptance',{}).get('ready'))==approved
    if approved:checks['formal_review_receipt']=bool(b.get('acceptance',{}).get('review_receipt_id'))
    if approved and reviews:
        events=b.get('plain_events') if scenario=='fallback' else b.get('approval_events',[])
        decisions=[event for event in events if event['phase']=='decision_received']
        ready=[event for event in events if event['phase']=='input_ready']
        checks['one_current_owner_decision']=len(decisions)==1 and bool(ready) and decisions[0]['monotonic']>ready[-1]['monotonic'] and decisions[0]['review_hash']==reviews[0]['body_sha256']
        receipt=b.get('acceptance',{}).get('receipt',{})
        checks['receipt_current_frozen_version']=receipt.get('plan_id')==reviews[0]['plan_id'] and receipt.get('plan_version')==reviews[0]['plan_version'] and receipt.get('submission_id')==reviews[0]['submission'].get('submission_id')
    if scenario=='semantic':checks['stale_input_rejected']=b.get('outcome',{}).get('type') in {'revision_required','reconsultation_required'}
    if scenario in {'feedback','reject'}:checks['feedback_not_approval']=b.get('outcome',{}).get('type')=='reconsultation_required'
    if scenario=='feedback':checks['multiline_feedback_is_one_input']=b.get('input')==[{'stage':'plan_review','response':'请保留当前输入，说明未决条件。\n/details\n这是同一反馈'}]
    if scenario=='reject':checks['reject_then_feedback']=b.get('input')==[{'stage':'plan_review','response':'n'},{'stage':'plan_review_feedback','response':'请按当前材料重新咨询，保留未决条件。'}]
    if scenario=='eof':checks['eof_preserves_waiting_without_decision']=b.get('outcome',{}).get('type')=='approval_pending' and not b.get('input') and not any(e['phase']=='decision_received' for e in b.get('approval_events',[]))
    if scenario=='cancel':checks['cancel_reaches_real_terminal']=b.get('outcome',{}).get('type')=='terminal' and b['outcome'].get('payload',{}).get('status')=='cancelled'
    return {'passed':all(checks.values()),'checks':checks}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--export',required=True);p.add_argument('--results',required=True);p.add_argument('--mode',choices=['tty','plain'],default='tty');p.add_argument('--scenario',default='approve')
    a=p.parse_args();checks=record(a.export,a.results,a.mode,a.scenario);raise SystemExit(0 if checks['passed'] else 1)
