import asyncio
import concurrent.futures
import io
from unittest.mock import patch
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.application import create_app_session
from agent_core.cli_terminal import Terminal


def test_buffer_paste_identity_and_approval_snapshot():
    async def check():
        owner=Terminal()
        owner.pending=concurrent.futures.Future()
        future=owner.pending
        owner.buffer.text='甲'*300+'\n'+'乙'*300
        owner.buffer.cursor_position=len(owner.buffer.text)
        owner.buffer.delete_before_cursor(300)
        owner.submit()
        assert future.result()=='甲'*300+'\n'
        original={'object':'v1','command':'echo 准确命令','path':'/tmp/中文输出'}
        owner.set_context('task','code_review',original)
        original['object']='v2'
        assert owner.context[2]['object']=='v1'
        owner.buffer.text='n'
        owner.pending=concurrent.futures.Future(); future=owner.pending
        owner.submit()
        assert future.result()=='n'  # No implicit approval or rebinding.
        owner.closed=True
        if owner.history_flush:
            await owner.history_flush
    with create_pipe_input() as pipe, create_app_session(input=pipe,output=DummyOutput()):
        asyncio.run(check())


def test_cancel_unknown_confirmation_no_repeat():
    async def check():
        owner=Terminal(); calls=[]
        owner.cancel_callback=lambda:calls.append('cancel')
        owner.request_cancel();owner.request_cancel()
        assert calls==['cancel']
        assert '未确认' in owner.model.status()
        assert all('安全停止完成' not in x for x in owner.model.history)
        owner.closed=True
        if owner.history_flush: await owner.history_flush
    with create_pipe_input() as pipe, create_app_session(input=pipe,output=DummyOutput()):
        asyncio.run(check())


def test_tick_render_without_state_imports():
    async def check():
        owner=Terminal()
        before=owner.model.updates
        for _ in range(1000):
            owner.model.status(); owner._rendered()
        assert owner.model.updates==before and owner.frames==1000
        assert not any(name.endswith('plan_tracker') for name in __import__('sys').modules)
        owner.closed=True
    with create_pipe_input() as pipe, create_app_session(input=pipe,output=DummyOutput()):
        asyncio.run(check())


def test_real_interrupt_loop_ui_on_off_preserves_stale_approval_rejection():
    import ast, time, types
    from pathlib import Path
    from agent_core.cli_display import bind_terminal, DisplayModel, display_print
    source=(Path(__file__).parents[1]/'bio_agents.py').read_text()
    tree=ast.parse(source)
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='BioAnalystAgent')
    fn=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_run_agent_interrupt_loop')
    class Command:
        def __init__(self,resume):self.resume=resume
    class Colors:
        def __getattr__(self,key):return ''
    namespace={'asyncio':asyncio,'time':time,'display_print':display_print,'Fore':Colors(),'Style':Colors(),'Command':Command,'timestamp_now':lambda:'fixture','output':types.SimpleNamespace(page_text=lambda *a:display_print(a[0]))}
    module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),fn],type_ignores=[])
    exec(compile(ast.fix_missing_locations(module),'bio_agents.py:interrupt-loop','exec'),namespace)
    class Backend:
        def __init__(self):
            self.calls=[];self.count=0;self.state_manager=None
            self.profiler=types.SimpleNamespace(finish_task=lambda *a,**kw:None)
        async def go(self,value,**kwargs):
            self.count+=1
            if isinstance(value,Command):
                self.calls.append(('resume',value.resume,'stale_object_rejected'))
            else:self.calls.append(('route','managed'))
        def _app_for_task(self,task):return self
        def get_state(self,config):
            payload={'type':'code_review','object':'v1','content':'echo fixture','risk':{'level':'LOW'}}
            tasks=[types.SimpleNamespace(interrupts=[types.SimpleNamespace(value=payload)])] if self.count==1 else []
            return types.SimpleNamespace(tasks=tasks,values={},next=())
        async def _aprompt_user(self,*args,**kwargs):
            self.calls.append(('approval_displayed',kwargs['payload']['object']))
            # Backend object changes while old UI is displayed; response remains y,
            # and go's existing boundary rejects the stale object.
            return 'y'
        def _record_human_wait(self,*args,**kwargs):self.calls.append(('human_response',args[3]))
        def _terminal_status_from_snapshot(self,*args):return 'paused_retryable'
        def _snapshot_values(self,snapshot):return snapshot.values
        def _typed_inspection_terminal_projection(self,*args):return {}
        def _typed_generate_terminal_projection(self,*args):return {}
        def _interactive_run_result(self,task,status,snapshot):return status
    Backend.run=namespace['_run_agent_interrupt_loop']
    outcomes=[]; previous=bind_terminal(None)
    try:
        for owner in [None,DisplayModel()]:
            bind_terminal(owner);backend=Backend()
            result=asyncio.run(backend.run('task','fixture'))
            outcomes.append((result,backend.calls))
        assert outcomes[0]==outcomes[1]
        assert outcomes[1][1][-1]==('resume','y','stale_object_rejected')
    finally:bind_terminal(previous)
