"""Real PTY/Application replay of synthetic inputs through production producers.

The recording is output from a kernel PTY, not a DummyOutput demonstration.
A small VT text decoder makes inspectable last-screen snapshots; raw bytes and
asciicast remain available, including a list of unsupported control sequences.
"""
import argparse
import asyncio
import codecs
import concurrent.futures
import errno
import fcntl
import hashlib
import html
import inspect
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time

PYTHON='/public/home/liusiqi/anaconda3/envs/biomind/bin/python'
DEPS='/tmp/biomind-cli-deps'


def pulse(loop):
    def service():
        if not loop.is_closed():loop.call_later(.002,service)
    service()


def atomic_json(path,value):
    temporary=path.with_suffix('.new')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str))
    temporary.replace(path)


async def synthetic_producers(owner, root, scenario):
    """Invoke actual observer/router/tool/approval functions on fixed inputs."""
    import bio_agents
    from agent_core.cli_display import emit_public,flush_public
    from agent_core.public_response import project_public_json
    from agent_core.cli_activity import call_with_activity
    from unittest.mock import patch
    counts={'model_calls':0,'tool_calls':0,'executions':0,'approvals':0,'rejected':0,'cancel_requests':0}
    callbacks=[];requests=[]
    router=bio_agents.OutputRouter()
    agent=bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
    agent.planner_progress_enabled=True
    agent.planner_progress_callback=lambda event,row:callbacks.append([event,row])
    async def public(text):
        # Fixed fake response uses the same production response projection and
        # public outlet as the current C/P node, with no narration-only call.
        raw=json.dumps({'decision':'synthetic_fixture','public_message':text},ensure_ascii=False)
        requests.append({'response':raw});counts['model_calls']+=1
        projected=project_public_json(raw)
        emit_public(projected.public_message,task_id='v2-synthetic',node='planner',call_id='round-1',source_message_id=f'model-{counts["model_calls"]}')
        await flush_public()
    try:
        with patch.object(bio_agents,'output',router):
            await public('这是隔离合成回放。我先读取已创建的三份输入，再检查匹配结果。')
            atomic_json(root/'phase.json',{'phase':'interaction_ready'})
            while not (root/'proceed').exists():await asyncio.sleep(.01)
            for index in range(30):
                agent._planner_progress('v2-synthetic','attempt_progress',{'candidate_ordinal':1,
                    'content_char_count':index*37,'elapsed_seconds':index/10,'payload_idle_seconds':.1,
                    'observation_at':f'fixed-{index}'})
            for phase in ['parse_envelope','normalize_candidate','compile_plan']:
                agent._planner_observed_call('v2-synthetic',{'candidate_ordinal':1},phase,json.loads,'{"fixture":true}')
            if scenario=='approval':
                from agent_core.middleware import HITLMiddleware
                import agent_core.middleware as middleware
                from copy import deepcopy
                code='\n'.join(['# 合成批准对象；拒绝前可查看完整正文']+[f'printf "line-{index}\\n"' for index in range(45)])
                agent.input_timeout_seconds=45;agent.data_path=str(root/'scratch');agent.state_manager=None
                approvals=[]
                def present_interrupt(payload):
                    original=deepcopy(payload)
                    if hasattr(router,'prepare_review'):
                        router.prepare_review('代码审批',payload['content'],language=payload['language'],risk=payload['risk']['risk_level'])
                    else:
                        router.page_text('Code Review',payload['content'])
                    atomic_json(root/'phase.json',{'phase':'approval_pending'})
                    # Actual async UI entry sets context before its sync input
                    # bridge; preserve that existing precondition on base too.
                    owner.set_context('v2-synthetic','code_review',payload)
                    response=agent._prompt_user('v2-synthetic','code_review','[y=run, n=reject, e:<code>=edit] > ',payload=payload)
                    assert payload==original
                    approvals.append({'response':response,'payload':payload,'code_sha256':hashlib.sha256(code.encode()).hexdigest()})
                    return response
                with patch.object(middleware,'interrupt',side_effect=present_interrupt),patch.object(bio_agents,'clear_waiting_state',lambda *a:None):
                    decision,result=HITLMiddleware(auto_approve_safe=False).review_code(code,'bash')
                counts['approvals']=len(approvals);counts['rejected']=int(decision=='reject')
                atomic_json(root/'approval.json',{'requests':approvals,'action':decision,'result':result,'executions':0})
                def cancelled():counts['cancel_requests']+=1
                owner.cancel_callback=cancelled
                atomic_json(root/'phase.json',{'phase':'cancel_ready'})
                while not owner.cancel_requested:await asyncio.sleep(.01)
                await public('脚本已被拒绝，本场景没有派发执行。取消请求仍按原终端语义处理。')
            else:
                inputs=root/'fixture-inputs';inputs.mkdir(exist_ok=True)
                for index in range(3):
                    path=inputs/f'中文输入_{index}.txt';path.write_text('synthetic input\n')
                    scope={'task_id':'v2-synthetic','node':'planner','call_id':'round-1','tool_call_id':f'read-{index}','tool_name':'read','tool_category':'read'}
                    call_with_activity('读取',path.read_text,display_scope=scope);counts['tool_calls']+=1
                call_with_activity('搜索',lambda:list(inputs.glob('*.txt')),display_scope={
                    'task_id':'v2-synthetic','node':'planner','call_id':'round-1','tool_call_id':'search-1','tool_name':'search','tool_category':'search'})
                counts['tool_calls']+=1
                empty_kwargs={'display_result':lambda value:'empty'} if 'display_result' in inspect.signature(call_with_activity).parameters else {}
                empty=call_with_activity('搜索',lambda:list(inputs.glob('*.missing')),display_scope={
                    'task_id':'v2-synthetic','node':'planner','call_id':'round-1','tool_call_id':'search-empty','tool_name':'search','tool_category':'search'},**empty_kwargs)
                assert empty==[];counts['tool_calls']+=1;counts['empty_results']=1
                await public('三份合成输入均已返回，搜索得到三个匹配项，另一项搜索返回空结果。没有调用真实模型或科学分析流程。')
                if scenario=='failure':
                    # These are explicitly synthetic recorded facts, presented
                    # through their real production output outlets. They do not
                    # prove the route of the user's original session.
                    from execution.executor import run_script_path
                    script=root/'scratch/synthetic.sh'
                    script.write_text("printf 'checked synthetic inputs\\n'\n"+
                        "printf 'BIOMIND_EFFECT_'; printf 'EVIDENCE_JSON={\"fixture\":true}\\n'\n")
                    digest=hashlib.sha256(script.read_bytes()).hexdigest()
                    allowed=sys.modules['__main__'].OFFLINE_ALLOWED_SCRIPT_PATHS
                    allowed.add(str(script))
                    try:
                        raw,stderr,rc=await run_script_path(str(script),expected_sha256=digest,
                            interpreter='bash',profile=False,env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'},
                            display_context={'task_id':'v2-synthetic','action_id':'synthetic-execute','execution_attempt_id':'attempt-1'})
                    finally:allowed.discard(str(script))
                    counts['executions']+=1
                    atomic_json(root/'raw-receipt.json',{'script_sha256':digest,'stdout':raw,
                        'stdout_sha256':hashlib.sha256(raw.encode()).hexdigest(),'stderr':stderr,'returncode':rc})
                    assert rc==0
                    if 'summary' in inspect.signature(router.print_observation).parameters:
                        router.print_observation(raw,item_id='fixture-output',summary='进程已退出 · rc=0；正式核验中')
                    else:router.print_observation(raw)
                    from supervisor import SupervisorNode
                    from tests.test_inspection_workflow import make_inspection_fixture
                    from copy import deepcopy
                    fixture=make_inspection_fixture(root/'supervisor-fixture')
                    local=await SupervisorNode(fixture.reviewer,output_dir=str(root/'supervisor-output')).run(deepcopy(fixture.state))
                    counts['model_calls']+=len(fixture.reviewer.requests)
                    assert local['validation_evidence']['status']=='VALID'
                    router.render_terminal_result(bio_agents.InteractiveRunResult(task_id='v2-synthetic',status='failed',
                        stage='supervisor',failure_code='STEP_EFFECT_INCOMPLETE',reason='合成正式回执：产物证据不完整；尚未提交。'))
                    await public('现有正式回执表明产物证据不完整。我建议检查产物声明；这段模型说明不决定路由。')
                    agent._planner_progress('v2-synthetic','candidate_started',{'candidate_ordinal':1,'nominal_call_limit':3,'observation_at':'replan'})
            atomic_json(root/'business.json',{'synthetic':True,'scenario':scenario,'counts':counts,
                'producer_callbacks':callbacks,'fake_responses':requests,'session_route_evidence':False,
                'producer_functions':['BioAnalystAgent._planner_progress','BioAnalystAgent._planner_observed_call',
                    'project_public_json','cli_activity.call_with_activity','OutputRouter.print_observation',
                    'OutputRouter.render_terminal_result','SupervisorNode.run','executor.run_script_path',
                    'HITLMiddleware.review_code','HITLMiddleware._parse_code_response','BioAnalystAgent._prompt_user']})
            atomic_json(root/'phase.json',{'phase':'business_done'})
    except BaseException as exc:
        atomic_json(root/'producer-error.json',{'type':type(exc).__name__,'message':str(exc)})
        raise


def test_v2_real_pty_replay():
    import pytest
    if os.environ.get('BIOMIND_V2_PTY')!='1':pytest.skip('explicit guarded PTY recording only')
    sys.stdout=sys.__stdout__;sys.stderr=sys.__stderr__
    assert sys.stdin.isatty() and sys.stdout.isatty()
    os.write(1,b'\x1b[2J\x1b[H\x1b]133;A;biomind-replay-start\x07')
    root=Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS'])
    scenario=os.environ['BIOMIND_V2_SCENARIO'];verbosity=os.environ['BIOMIND_CLI_VERBOSITY']
    from agent_core.cli_terminal import Terminal
    from agent_core.cli_display import bind_terminal
    async def run():
        owner=Terminal(**({'verbosity':verbosity} if 'verbosity' in inspect.signature(Terminal).parameters else {}))
        previous=bind_terminal(owner)
        original=owner._rendered
        def rendered():
            original()
            atomic_json(root/'frame-state.json',{'frames':owner.frames,'modal':owner.modal,
                'pending':owner.pending is not None,'draft':owner.buffer.text,
                'size':list(os.get_terminal_size(1))})
        owner._rendered=rendered
        pulse(asyncio.get_running_loop())
        completion=concurrent.futures.Future()
        def worker():
            async def invoke():
                pulse(asyncio.get_running_loop())
                await synthetic_producers(owner,root,scenario)
            try:asyncio.run(invoke())
            except BaseException as exc:completion.set_exception(exc)
            else:completion.set_result(None)
        async def finish():
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(completion)),60)
                while owner.modal:await asyncio.sleep(.01)
                if hasattr(owner,'finish_tools'):owner.finish_tools()
                owner._changed()
                while owner.history_flush is not None or owner.history_cursor<len(owner.model.items):await asyncio.sleep(.01)
                owner.app.invalidate();frame=owner.frames
                while owner.frames==frame:await asyncio.sleep(.01)
                items=[{'kind':t.kind,'source':t.source,'text':t.text,'scope':t.scope,'times':t.times,
                    'presentation':getattr(t,'presentation','history')} for t in owner.model.items]
                atomic_json(root/'renderer.json',{'items':items,'details':list(owner.model.details),'status':owner.model.status(),
                    'frames':owner.frames,'draft':owner.buffer.text,'size':list(os.get_terminal_size(1))})
                atomic_json(root/'phase.json',{'phase':'snapshot_ready'})
                while not (root/'capture_done').exists():await asyncio.sleep(.01)
            finally:
                owner.app.exit()
        tasks=[]
        def start():
            threading.Thread(target=worker,name='v2-synthetic-runtime',daemon=True).start()
            tasks.append(asyncio.create_task(finish()))
        try:
            await owner.app.run_async(pre_run=start,set_exception_handler=False)
            await tasks[0]
        finally:owner.closed=True;bind_terminal(previous)
    asyncio.run(run())


class VTTextScreen:
    """Decode the VT cursor/erase operations emitted by prompt_toolkit.

    Styles are intentionally omitted from the text/SVG snapshot. Unsupported
    commands are recorded so a screen is never silently claimed complete.
    """
    def __init__(self,cols,rows):
        sys.path.insert(0,DEPS)
        from wcwidth import wcwidth
        self.width=wcwidth;self.cols=cols;self.rows=rows;self.x=0;self.y=0
        self.grid=[[' ']*cols for _ in range(rows)];self.history=[];self.pending='';self.unknown=[];self.saved=(0,0)
    def resize(self,cols,rows):
        self.grid=[(row+[' ']*cols)[:cols] for row in self.grid[:rows]]
        self.grid += [[' ']*cols for _ in range(rows-len(self.grid))]
        self.cols,self.rows=cols,rows;self.x=min(self.x,cols-1);self.y=min(self.y,rows-1)
    def down(self):
        self.y+=1
        if self.y>=self.rows:
            self.history.append(''.join(self.grid.pop(0)).rstrip());self.grid.append([' ']*self.cols);self.y=self.rows-1
    def feed(self,text):
        self.pending+=text;replies=[]
        while self.pending:
            if self.pending.startswith('\x1b['):
                match=re.match(r'\x1b\[([0-?]*)([ -/]*)([@-~])',self.pending)
                if not match:break
                args,inter,op=match.groups();self.pending=self.pending[match.end():]
                nums=[int(v) if v else 0 for v in args.lstrip('?').split(';')];n=nums[0] or 1
                if op=='A':self.y=max(0,self.y-n)
                elif op=='B':self.y=min(self.rows-1,self.y+n)
                elif op=='C':self.x=min(self.cols-1,self.x+n)
                elif op=='D':self.x=max(0,self.x-n)
                elif op in 'Hf':self.y=min(self.rows-1,(nums[0] or 1)-1);self.x=min(self.cols-1,(nums[1] if len(nums)>1 and nums[1] else 1)-1)
                elif op=='G':self.x=min(self.cols-1,n-1)
                elif op=='d':self.y=min(self.rows-1,n-1)
                elif op=='K':
                    left=0 if nums[0] in (1,2) else self.x;right=self.cols if nums[0] in (0,2) else self.x+1
                    self.grid[self.y][left:right]=[' ']*(right-left)
                elif op=='J':
                    if nums[0] in (2,3):self.grid=[[' ']*self.cols for _ in range(self.rows)]
                    elif nums[0]==0:
                        self.grid[self.y][self.x:]=[' ']*(self.cols-self.x)
                        for row in range(self.y+1,self.rows):self.grid[row]=[' ']*self.cols
                    else:self.unknown.append(match.group())
                elif op=='n' and nums[0]==6:replies.append(f'\x1b[{self.y+1};{self.x+1}R')
                elif op in 'mhlq':pass
                elif op=='s':self.saved=(self.x,self.y)
                elif op=='u':self.x,self.y=self.saved
                else:self.unknown.append(match.group())
                continue
            if self.pending.startswith('\x1b]'):
                end=re.search('\x07|\x1b\\\\',self.pending)
                if not end:break
                self.pending=self.pending[end.end():];continue
            if self.pending[0]=='\x1b':
                if len(self.pending)<2:break
                op=self.pending[1];self.pending=self.pending[2:]
                if op=='7':self.saved=(self.x,self.y)
                elif op=='8':self.x,self.y=self.saved
                elif op in '=>':pass
                else:self.unknown.append('\x1b'+op)
                continue
            c=self.pending[0];self.pending=self.pending[1:]
            if c=='\r':self.x=0
            elif c=='\n':self.down()
            elif c=='\b':self.x=max(0,self.x-1)
            elif c=='\t':self.x=min(self.cols-1,(self.x//8+1)*8)
            elif ord(c)<32:pass
            else:
                width=max(0,self.width(c))
                if width==0:continue
                if self.x+width>self.cols:self.x=0;self.down()
                self.grid[self.y][self.x]=c
                if width==2:self.grid[self.y][self.x+1]=''
                self.x+=width
        return replies
    def text(self):return '\n'.join(''.join(row).rstrip() for row in self.grid)


def snapshot(root,name,screen):
    from PIL import Image,ImageDraw,ImageFont
    image=Image.new('RGB',(screen.cols*10+24,screen.rows*20+24),'#17191c')
    draw=ImageDraw.Draw(image);font=ImageFont.truetype('/usr/share/fonts/google-noto-cjk/NotoSansCJK-DemiLight.ttc',16)
    for y,row in enumerate(screen.grid):
        for x,char in enumerate(row):
            if char and char!=' ':draw.text((12+x*10,9+y*20),char,font=font,fill='#e6e7e8')
    image.save(root/(name+'.screen.png'))
    text=screen.text();(root/(name+'.screen.txt')).write_text(text)
    width,height=screen.cols*9+24,screen.rows*19+24
    rows=''.join(f'<text x="12" y="{20+19*i}">{html.escape(line)}</text>' for i,line in enumerate(text.splitlines()))
    (root/(name+'.screen.svg')).write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="#17191c"/><g fill="#e6e7e8" font-family="monospace" font-size="15" xml:space="preserve">{rows}</g></svg>')


def record(source,root,scenario,cols,rows,verbosity,resize=None,term='xterm-256color'):
    source=source.resolve();root=root.resolve()
    assert source.is_relative_to(Path('/tmp')) and root.is_relative_to(Path('/tmp'))
    manifest=json.loads((source/'export_manifest.json').read_text())
    assert all(hashlib.sha256((source/name).read_bytes()).hexdigest()==digest for name,digest in manifest['files'].items())
    root.mkdir(parents=True,exist_ok=False);(root/'scratch').mkdir()
    env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8','PYTHONPATH':DEPS,'TERM':term,
        'COLUMNS':str(cols),'LINES':str(rows),'PYTHONDONTWRITEBYTECODE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1',
        'TMPDIR':str(root/'scratch'),'BIOMIND_WIKI_ENABLED':'0','LOG_LEVEL':'CRITICAL',
        'BIOMIND_OFFLINE_RESULTS':str(root/'pytest-results.json'),'BIOMIND_CLI_PROGRESS_ARTIFACTS':str(root),
        'BIOMIND_GER_ARTIFACTS':str(root),'BIOMIND_EXEC_LOG_DIR':str(root/'scratch/executor-logs'),
        'BIOMIND_V2_PTY':'1','BIOMIND_V2_SCENARIO':scenario,'BIOMIND_CLI_VERBOSITY':verbosity}
    # The committed guard executes the existing audited bootstrap. Its initial
    # harmless synthetic-config printout is kept in raw recording as provenance.
    command=[PYTHON,str(source/'tests/cli_model_progress_support.py'),'--guarded-child',str(source),
        '-q','-s','tests/replay_cli_v2_pty.py::test_v2_real_pty_replay']
    master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',rows,cols,0,0))
    child=subprocess.Popen(command,cwd=source,env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
    os.close(slave);screen=VTTextScreen(cols,rows);decode=codecs.getincrementaldecoder('utf-8')('replace')
    started=time.monotonic();actions=[];seen=set();resize_done=False;modal_opened=False
    recording=False
    raw=(root/'terminal.raw').open('wb');cast=(root/'terminal.cast').open('w')
    cast.write(json.dumps({'version':2,'width':cols,'height':rows,'timestamp':int(time.time()),'title':'BioMind synthetic production PTY replay'})+'\n')
    def send(data,label):
        os.write(master,data.encode());actions.append({'at':time.monotonic()-started,'action':label})
    def read_json(name):
        try:return json.loads((root/name).read_text())
        except (OSError,ValueError):return {}
    try:
        while time.monotonic()-started<90:
            if select.select([master],[],[],.02)[0]:
                try:data=os.read(master,65536)
                except OSError as exc:
                    if exc.errno==errno.EIO:break
                    raise
                if not data:break
                raw.write(data);raw.flush();text=decode.decode(data)
                marker='\x1b]133;A;biomind-replay-start\x07'
                if marker in text:
                    recording=True;text_for_cast='\x1b[2J\x1b[H'+text.split(marker,1)[1]
                else:text_for_cast=text
                if recording and 'captured' not in seen:
                    cast.write(json.dumps([time.monotonic()-started,'o',text_for_cast],ensure_ascii=False)+'\n');cast.flush()
                for reply in screen.feed(text):send(reply,'cursor_position_report')
            phase=read_json('phase.json').get('phase');frame=read_json('frame-state.json')
            if phase=='interaction_ready' and 'draft' not in seen:
                send('中文草稿\x0a第二行 /tmp/合成长路径/输入文件.txt','multiline_draft');send('\x1bOQ','F2_details');seen.add('draft')
            if phase=='interaction_ready' and frame.get('modal') and 'proceed' not in seen:
                snapshot(root,'initial-details',screen);modal_opened=True
                if scenario=='approval':
                    if 'initial-closing' not in seen:send('\x1b','close_initial_details');seen.add('initial-closing')
                else:(root/'proceed').write_text('continue fixture');seen.add('proceed')
            if phase=='interaction_ready' and scenario=='approval' and 'initial-closing' in seen and not frame.get('modal') and 'proceed' not in seen:
                (root/'proceed').write_text('continue fixture');seen.add('proceed')
            if resize and 'proceed' in seen and not resize_done:
                newcols,newrows=resize;fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',newrows,newcols,0,0));screen.resize(newcols,newrows)
                os.kill(child.pid,signal.SIGWINCH);actions.append({'at':time.monotonic()-started,'resize':[newcols,newrows]});resize_done=True
            if phase=='approval_pending' and frame.get('pending') and not frame.get('modal') and 'approval-details' not in seen and 'approval-captured' not in seen:
                send('\x1bOQ','approval_F2');seen.add('approval-details')
            if phase=='approval_pending' and frame.get('pending') and frame.get('modal') and 'approval-captured' not in seen:
                snapshot(root,'approval-details',screen);send('\x1b[6~','approval_page_down');send('\x1b','approval_Escape');seen.add('approval-captured')
            if phase=='approval_pending' and not frame.get('modal') and 'approval-captured' in seen and 'rejected' not in seen:
                send('n\r','approval_reject');seen.add('rejected')
            if phase=='cancel_ready' and 'cancel' not in seen:send('\x03','CtrlC_cancel');seen.add('cancel')
            if phase=='business_done' and 'done' not in seen:
                snapshot(root,'business-details',screen);send('\x1b','Escape_to_input');seen.add('done')
            if phase=='snapshot_ready' and 'captured' not in seen:
                snapshot(root,'final',screen);(root/'capture_done').write_text('captured');seen.add('captured')
            if child.poll() is not None:break
        if child.poll() is None:child.terminate()
        returncode=child.wait(timeout=5)
    finally:
        raw.close();cast.close();os.close(master)
        if child.poll() is None:child.kill();child.wait()
    business=read_json('business.json');render=read_json('renderer.json')
    history=[row for row in render.get('items',[]) if row.get('presentation')=='history']
    metrics={'returncode':returncode,'source':manifest['source'],'source_signature':manifest['source_signature'],
        'scenario':scenario,'verbosity':verbosity,'initial_size':[cols,rows],'resize':resize,
        'actual_pty':True,'production_application':True,'counts':business.get('counts'),
        'history_items':len(history),'permanent_logical_lines':sum(len(row['text'].splitlines()) or 1 for row in history),
        'model_paragraphs':sum(row['kind']=='model_public' for row in history),
        'tool_history_items':sum(row['kind'] in ('tool_start','tool_end') for row in history),
        'diagnostic_items':len(render.get('details',[])),'actions':actions,'unsupported_vt':sorted(set(screen.unknown)),
        'captured': 'captured' in seen,'modal_opened':modal_opened,'session_route_evidence':False,
        'replay_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    atomic_json(root/'metrics.json',metrics)
    (root/'invocation.json').write_text(json.dumps({'command':command,'environment':env},indent=2))
    print(json.dumps(metrics,ensure_ascii=False))
    return returncode if 'captured' in seen else 2


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export',type=Path,required=True);parser.add_argument('--results',type=Path,required=True)
    parser.add_argument('--scenario',choices=['normal','failure','approval'],required=True)
    parser.add_argument('--cols',type=int,default=80);parser.add_argument('--rows',type=int,default=24)
    parser.add_argument('--verbosity',choices=['compact','verbose'],default='compact')
    parser.add_argument('--resize');parser.add_argument('--term',default='xterm-256color')
    args=parser.parse_args();resize=tuple(map(int,args.resize.split('x'))) if args.resize else None
    raise SystemExit(record(args.export,args.results,args.scenario,args.cols,args.rows,args.verbosity,resize,args.term))
if __name__=='__main__':main()
