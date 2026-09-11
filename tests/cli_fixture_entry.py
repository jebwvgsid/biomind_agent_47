"""Offline harness executing exact CLI entry + interactive_mode source with a fake agent.

No import of BioMind configuration/startup/providers. AST selection keeps the
production entry functions unchanged while replacing backend dependencies.
"""
import asyncio
import ast
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time
import types
from dataclasses import dataclass

from agent_core.cli_display import display_print, emit as emit_display, terminal as active_terminal
from agent_core.cli_logging import configure_logging
from agent_core.human_io import HumanInputUnavailable, safe_input, safe_multiline_input

root = Path(__file__).resolve().parents[1]
if '03.毕设' in str(root):
    raise RuntimeError('Fixture must run from isolated export')
# Deny network, credential/startup reads and original workspace access.
def audit(event, args):
    if event == 'socket.connect':
        raise RuntimeError('offline fixture: network denied')
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = os.fsdecode(args[0]); name = Path(path).name
        if '03.毕设' in path or name.startswith('.env') or name in {'启动.sh', 'startup.py'}:
            raise RuntimeError('offline fixture: prohibited path')
sys.addaudithook(audit)
logger = logging.getLogger('BioMind.Agent')
configure_logging(logging.INFO, Path('runtime.log'))
class Colors:
    def __getattr__(self, key): return ''
Fore = Style = Colors()
PathManager = types.SimpleNamespace(WORK_DIR=str(Path.cwd()))
LLM_DEFAULT_MODEL = 'fake-only'
@dataclass
class InteractiveRunResult:
    task_id: str = ''
    status: str = ''
    stage: str = ''
    failure_code: str = ''
    reason: str = ''
    result_path: str = ''
    trace_path: str = ''

def restore_terminal_state(): pass
def _reset_interrupt_signal_guard(): pass

tree = ast.parse((root/'bio_agents.py').read_text())
cls = next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='BioAnalystAgent')
methods = [n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='interactive_mode']
selected = [n for n in tree.body if (isinstance(n,ast.ClassDef) and n.name=='OutputRouter') or (isinstance(n,ast.AsyncFunctionDef) and n.name in {'main','_runtime_main'})]
exec(compile(ast.Module(body=selected,type_ignores=[]),'bio_agents.py:exact-cli-entry','exec'))
class FakeAgent:
    def __init__(self, **kwargs):
        self.cancel_count=0
        owner=active_terminal()
        if owner:
            def install():
                owner.fixture_input_events=[]
                owner.buffer.on_text_changed += lambda buffer: owner.fixture_input_events.append((time.perf_counter_ns(),buffer.text))
            owner.loop.call_soon_threadsafe(install)
        if os.getenv('BIOMIND_FIXTURE_LOG_BURST')=='1':
            def noise():
                for _ in range(576):
                    logging.getLogger('BioMind.Performance').info('phase=state.load fixture=latency')
                    time.sleep(.003)
            threading.Thread(target=noise,daemon=True).start()
    async def aclose(self):
        owner=active_terminal()
        if owner:
            Path('ui-metrics.json').write_text(json.dumps({'frames':owner.frames,'history_entries':len(owner.model.history),'updates':owner.model.updates,'draft':owner.buffer.text,'input_events':getattr(owner,'fixture_input_events',[])},ensure_ascii=False))
    def _should_auto_rename_session(self,*args,**kwargs): return False
    async def _aprompt_user(self, task_id, stage, prompt, payload=None, multiline=False, **kwargs):
        owner=active_terminal()
        if owner:
            if stage=='interactive_prompt' and Path('submitted.json').exists():
                Path('draft-returned.json').write_text(json.dumps(owner.buffer.text,ensure_ascii=False))
            owner.set_context(task_id,stage,payload)
        return (safe_multiline_input if multiline else safe_input)(prompt,0)
    async def _run_agent_interrupt_loop(self, text, task_id, **kwargs):
        Path('submitted.json').write_text(json.dumps(text,ensure_ascii=False))
        output.reset_for_task()
        owner=active_terminal()
        current=asyncio.current_task(); loop=asyncio.get_running_loop()
        if owner:
            def cancel():
                self.cancel_count+=1
                Path('cancel_count.json').write_text(str(self.cancel_count))
                loop.call_soon_threadsafe(current.cancel)
            owner.cancel_callback=cancel
        output.print_node_header('consultant')
        emit_display('message','已检索到 7 个候选流程（fake fixture）')
        try:
            for i in range(576 if text not in {'approve','exception','renderer-failure'} else 2):
                logging.getLogger('BioMind.Performance').info('phase=state.load status=completed elapsed_ns=42')
                emit_display('activity','等待 fake provider 返回')
                await asyncio.sleep(30/576)
            if text=='exception':
                raise RuntimeError('isolated fake runtime exception')
            if text=='renderer-failure' and owner:
                def break_renderer():
                    def broken(*args,**kwargs):
                        raise RuntimeError('fixture renderer failure')
                    owner.app.renderer.render=broken
                    owner.app.invalidate()
                owner.loop.call_soon_threadsafe(break_renderer)
            emit_display('tool_start','工具执行（fake fixture）')
            emit_display('tool_end','命令 rc=0，等待正式核验')
            emit_display('error','可恢复失败：fake 验证拒绝，等待后端处理')
            if text=='approve':
                response=await self._aprompt_user(task_id,'code_review','批准对象 fixture-v1，命令 echo fixture，输出 /tmp/fixture [y/n/q] > ',payload={'object':'fixture-v1','command':'echo fixture','output':'/tmp/fixture'})
                Path('approval.json').write_text(json.dumps(response))
            return InteractiveRunResult(task_id=task_id,status='failed',stage='supervisor',failure_code='FIXTURE_REJECTED',reason='正式验证拒绝；rc=0 不等于通过')
        except asyncio.CancelledError:
            if text=='cancel-delayed':
                await asyncio.sleep(10)
            emit_display('cancel','provider fake 取消回执：CLEANUP_CONFIRMED；任务整体清理未确认。')
            raise

# Preserve original method body and globals, bind to fake backend class.
method_module=ast.Module(body=methods,type_ignores=[])
exec(compile(ast.fix_missing_locations(method_module),'bio_agents.py:exact-interactive-mode','exec'))
FakeAgent.interactive_mode=interactive_mode
BioAnalystAgent=FakeAgent
output=OutputRouter()
asyncio.run(main())
