"""Actual newline JSON service framing/concurrency/EOF on synthetic storage."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

HERE=Path(__file__).resolve().parent


class StdioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='biomind-pi-stdio-');self.root=Path(self.temp.name)
        self.proc=await asyncio.create_subprocess_exec(sys.executable,'-B',str(HERE/'stdio_fixture.py'),str(self.root),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        self.addAsyncCleanup(self.cleanup_process)
        self.counter=0;self.responses={};self.token=None
        hello=await self.call('hello');self.token=hello['host_token']
        await self.call('request.begin',{'request_id':'fixture','role':'generate'},host=True)

    async def cleanup_process(self):
        if self.proc.stdin and not self.proc.stdin.is_closing():self.proc.stdin.close()
        try:await asyncio.wait_for(self.proc.wait(),10)
        except asyncio.TimeoutError:self.proc.kill();await self.proc.wait()
        diagnostics=await self.proc.stderr.read()
        self.assertEqual(self.proc.returncode,0,diagnostics.decode(errors='replace')[-4000:])
        self.temp.cleanup()

    async def send(self,method,params=None,host=False):
        self.counter+=1;identity=str(self.counter)
        frame={'id':identity,'method':method,'params':params or {}}
        if method not in {'hello','request.begin'}:frame['params']={**frame['params'],'active_request_id':'fixture'}
        if host:frame['host_token']=self.token
        self.proc.stdin.write((json.dumps(frame)+'\n').encode());await self.proc.stdin.drain()
        return identity

    async def receive(self,identity):
        while identity not in self.responses:
            raw=await asyncio.wait_for(self.proc.stdout.readline(),30)
            self.assertTrue(raw,'domain service closed unexpectedly')
            row=json.loads(raw);self.responses[row['id']]=row
        row=self.responses.pop(identity)
        if 'error' in row:raise AssertionError(str(row['error']))
        return row['result']

    async def call(self,method,params=None,host=False):return await self.receive(await self.send(method,params,host))

    async def start(self,code):
        args={'call_id':'stdio-job','arguments':{'argv':[sys.executable,'-I','-u','-c',code]}}
        pending=await self.call('exec.start',args);approval=pending['approval']
        await self.call('approval.decide',{**{k:approval[k] for k in ('request_id','action_hash','request_scope')},'choice':'y'},host=True)
        return await self.call('exec.start',args)

    async def test_half_frame_then_concurrent_wait_cancel(self):
        raw=json.dumps({'id':'partial','method':'session.lifecycle','params':{}}).encode()
        self.proc.stdin.write(raw[:15]);await self.proc.stdin.drain()
        await asyncio.sleep(.03)
        self.proc.stdin.write(raw[15:]+b'\n');await self.proc.stdin.drain()
        self.assertFalse((await self.receive('partial'))['active_jobs'])
        result=await self.start('import time;print("stdio-alive",flush=True);time.sleep(3)')
        waiting=await self.send('exec.wait',{'run_ref':result['run_ref'],'max_wait_ms':60000})
        running=await asyncio.wait_for(self.call('session.lifecycle'),2)
        self.assertTrue(running['active_jobs'])
        cancelled=await asyncio.wait_for(self.call('exec.cancel',{'run_ref':result['run_ref']}),3)
        self.assertTrue(cancelled['cleanup_confirmed'])
        self.assertTrue((await self.receive(waiting))['terminal'])

    async def test_eof_does_not_cancel_real_independent_job(self):
        result=await self.start('import time;from pathlib import Path;time.sleep(.3);Path("eof-completed").write_text("done");print("real EOF receipt")')
        self.assertTrue(result['run_ref'])
        self.proc.stdin.close()
        await asyncio.wait_for(self.proc.wait(),10)
        self.assertEqual((self.root/'eof-completed').read_text(),'done')
        receipts=list((self.root/'data').rglob('*.json'))
        self.assertTrue(any('real EOF receipt' in p.read_text() for p in receipts))

    async def test_stdout_backpressure_does_not_block_target_cancellation(self):
        result=await self.start('import time;from pathlib import Path;time.sleep(.8);Path("must-not-complete").write_text("late")')
        queued=[await self.send('context.snapshot') for _ in range(100)]
        cancel=await self.send('exec.cancel',{'run_ref':result['run_ref']})
        # Intentionally stop draining stdout while Python processes the cancel.
        await asyncio.sleep(1.0)
        self.assertFalse((self.root/'must-not-complete').exists())
        self.assertTrue((await self.receive(cancel))['cleanup_confirmed'])
        for identity in queued:await self.receive(identity)

    async def test_lost_start_response_replay_and_new_explicit_call(self):
        args={'call_id':'lost-response','arguments':{'argv':[sys.executable,'-I','-u','-c',
            'from pathlib import Path;import time;p=Path("dispatch-count");p.write_text(p.read_text()+"x" if p.exists() else "x");time.sleep(.15);print("receipt")']}}
        async def approve(arguments):
            pending=await self.call('exec.start',arguments);a=pending['approval']
            await self.call('approval.decide',{**{k:a[k] for k in ('request_id','action_hash','request_scope')},'choice':'y'},host=True)
        async def terminal(result):
            for _ in range(100):
                if result.get('terminal'):return result
                result=await self.call('exec.wait',{'run_ref':result['run_ref'],'max_wait_ms':100})
            self.fail('STDIO synthetic job failed to reach terminal')
        await approve(args)
        lost_id=await self.send('exec.start',args)  # Receiver deliberately drops this response.
        replay=await self.call('exec.start',args)
        first=await terminal(replay)
        self.assertEqual((self.root/'dispatch-count').read_text(),'x')
        self.assertEqual(self.responses[lost_id]['result']['run_ref'],first['run_ref'])
        later={**args,'call_id':'new-explicit-call'}
        await approve(later);second=await terminal(await self.call('exec.start',later))
        self.assertNotEqual(second['run_ref'],first['run_ref'])
        self.assertEqual((self.root/'dispatch-count').read_text(),'xx')

    async def test_actual_product_cli_permission_positive_executor_and_scope_guard(self):
        self.proc.stdin.close();await asyncio.wait_for(self.proc.wait(),10)
        runtime=HERE.parent;repo=runtime.parents[1];started=time.monotonic()
        self.proc=await asyncio.create_subprocess_exec(sys.executable,'-B','-u',str(runtime/'python_service.py'),
            '--cwd',str(self.root),'--data-root',str(self.root/'production-data'),'--install-root',str(repo),
            '--session-id','product-cli-fixture','--registry-path',str(self.root/'registry.json'),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
            env={'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin','LC_ALL':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
        hello=await self.call('hello');self.token=hello['host_token']
        print('product CLI hello seconds',round(time.monotonic()-started,3),file=sys.stderr)
        await self.call('request.begin',{'request_id':'fixture','role':'generate'},host=True)
        args={'call_id':'actual-product','arguments':{'argv':[sys.executable,'-I','-u','-c',
            'from pathlib import Path;Path("actual-product-output").write_text("authorized");print("actual formal receipt")']}}
        result=await self.call('exec.start',args)
        self.assertEqual(result['status'],'waiting_approval')
        self.assertFalse((self.root/'actual-product-output').exists())
        approval=result['approval']
        await self.call('approval.decide',{**{k:approval[k] for k in ('request_id','action_hash','request_scope')},'choice':'y'},host=True)
        result=await self.call('exec.start',args)
        for _ in range(100):
            if result.get('terminal'):break
            result=await self.call('exec.wait',{'run_ref':result['run_ref'],'max_wait_ms':100})
        self.assertEqual(result['exit_code'],0)
        self.assertEqual((self.root/'actual-product-output').read_text(),'authorized')
        self.assertIn('actual formal receipt',result['stdout'])
        print('product policy: original formal permission/owner/executor; OS sandbox not claimed',file=sys.stderr)
        # send() fills the trusted fixture identity; send the stale frame explicitly.
        self.counter+=1;stale=str(self.counter)
        self.proc.stdin.write((json.dumps({'id':stale,'method':'files.write','params':{
            'active_request_id':'obsolete','call_id':'stale','path':'nope','content':'nope'}})+'\n').encode())
        await self.proc.stdin.drain()
        with self.assertRaisesRegex(AssertionError,'STALE_ACTIVE_REQUEST'):await self.receive(stale)


if __name__=='__main__':unittest.main(verbosity=2)
