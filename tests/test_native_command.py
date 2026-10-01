"""Local adapter tests independent of the Pi facade and user worktree changes."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

from agent_core.command_execution import CommandExecutionService
from agent_core.formal_command_observation import FormalCommandObserver
from agent_core.execution_profile import bind_host_profile
from agent_core.pending_input import mutate
from agent_core.plan_tracker import load_plan_state
from agent_core.request_state import request_scope
from agent_core.execution_permissions import PermissionDenied
from execution.executor import run_command


class NativeCommand(unittest.IsolatedAsyncioTestCase):
    async def test_same_observer_intent_replay_and_protected_public_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'A';external=root/'B';project.mkdir();external.mkdir()
            commands=CommandExecutionService(data_path=root/'state',project_dir=project,startup_dir=project,first_yield_ms=50)
            commands._execution_profile='native'
            observer=FormalCommandObserver(commands)
            host=SimpleNamespace(data_root=root/'state',task_id='native-unit',session_id='unit',cwd=project,
                execution_profile='native',active_jobs=lambda:[],formal=observer)
            bind_host_profile(host,'native')
            def request(state):state.setdefault('task_state',{}).update(pi_request_id='r',current_request_key='r',status='running')
            mutate(host.data_root,host.task_id,request)
            scope=request_scope(load_plan_state(host.data_root,host.task_id))
            args={'argv':[sys.executable,'-I','-B','-c',"from pathlib import Path;p=Path('effect');p.write_text(p.read_text()+'x' if p.exists() else 'x');print('observed')"],
                  'workdir':str(external)}
            result=await observer.start(args,task_id=host.task_id,request_scope=scope,call_id='same',yield_time_ms=1000)
            for _ in range(50):
                if result.get('terminal'):break
                result=await observer.wait_command({'run_ref':result['run_ref'],'max_wait_ms':100},task_id=host.task_id,request_scope=scope)
            self.assertEqual(result.get('exit_code'),0,result)
            replay=await observer.start(args,task_id=host.task_id,request_scope=scope,call_id='same')
            self.assertEqual(replay['run_ref'],result['run_ref']);self.assertEqual((external/'effect').read_text(),'x')
            self.assertEqual(load_plan_state(host.data_root,host.task_id)['task_state'].get('execution_permissions',{}).get('requests',{}),{})
            # The old public executor still requires the original permission seal.
            with self.assertRaisesRegex(PermissionDenied,'EXECUTION_PERMIT_REQUIRED'):
                await run_command('true',cwd=external)
            await observer.shutdown()


if __name__=='__main__':unittest.main()
