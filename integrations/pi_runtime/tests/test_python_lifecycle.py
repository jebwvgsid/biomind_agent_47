"""Real Python service crash and delayed old-scope receipts, synthetic jobs only.

These are L3 domain/transport assertions, not Pi branch-navigation evidence.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

RUNTIME=Path(__file__).resolve().parents[1]
REPO=RUNTIME.parents[1]


class ReplyError(Exception):pass


class ProductBridge:
    def __init__(self,root,session):
        self.root=root;self.session=session;self.request='original';self.sequence=0
        self.responses={};self.trace=[];self.token=None;self.proc=None

    async def open(self):
        self.proc=await asyncio.create_subprocess_exec(sys.executable,'-B','-u',str(RUNTIME/'python_service.py'),
            '--cwd',str(self.root),'--data-root',str(self.root/'data'),'--install-root',str(REPO),
            '--session-id',self.session,'--registry-path',str(self.root/'registry.json'),
            '--preferences-path',str(self.root/'preferences.md'),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
            env={'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin','LC_ALL':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
        self.token=(await self.call('hello'))['host_token']
        return self

    async def send(self,method,params=None,host=False,request=None):
        self.sequence+=1;identity=str(self.sequence);payload=dict(params or {})
        if method not in {'hello','request.begin'}:payload['active_request_id']=request or self.request
        frame={'id':identity,'method':method,'params':payload}
        if host:frame['host_token']=self.token
        self.proc.stdin.write((json.dumps(frame)+'\n').encode());await self.proc.stdin.drain()
        self.trace.append({'event':'sent','id':identity,'method':method,
            'active_request_id':payload.get('active_request_id'),'run_ref':payload.get('run_ref')})
        return identity

    async def receive(self,identity):
        while identity not in self.responses:
            raw=await asyncio.wait_for(self.proc.stdout.readline(),10)
            if not raw:raise AssertionError('service closed before expected response')
            frame=json.loads(raw);self.responses[frame['id']]=frame
            safe=dict(frame)
            if 'result' in safe:safe['result']={k:v for k,v in safe['result'].items() if k!='host_token'}
            self.trace.append({'event':'received',**safe})
        frame=self.responses.pop(identity)
        if 'error' in frame:raise ReplyError(frame['error']['message'])
        return frame['result']

    async def call(self,method,params=None,host=False,request=None):
        return await self.receive(await self.send(method,params,host,request))

    async def begin(self,identity='original'):
        result=await self.call('request.begin',{'request_id':identity,'role':'generate'},host=True)
        self.request=identity
        return result

    async def close(self):
        if self.proc.returncode is None:
            self.proc.stdin.close()
            try:await asyncio.wait_for(self.proc.wait(),10)
            except asyncio.TimeoutError:self.proc.kill();await self.proc.wait()
        return (await self.proc.stderr.read()).decode(errors='replace')


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='biomind-pi-lifecycle-');self.root=Path(self.temp.name)
        (self.root/'registry.json').write_text('{"environments":{},"tools":{}}')
        (self.root/'preferences.md').write_text('Synthetic lifecycle fixture')
        self.bridges=[]

    async def bridge(self,session='lifecycle'):
        bridge=await ProductBridge(self.root,session).open();self.bridges.append(bridge)
        await bridge.begin();return bridge

    async def asyncTearDown(self):
        for bridge in self.bridges:await bridge.close()
        self.temp.cleanup()

    async def marker(self,name):
        for _ in range(250):
            if (self.root/name).is_file():return (self.root/name).read_text()
            await asyncio.sleep(.02)
        self.fail('synthetic worker marker missing: '+name)

    async def start(self,bridge,code,call_id):
        arguments={'call_id':call_id,'arguments':{'argv':[sys.executable,'-I','-u','-c',code]}}
        pending=await bridge.call('exec.start',arguments);self.assertEqual(pending['status'],'waiting_approval')
        approval=pending['approval']
        await bridge.call('approval.decide',{**{k:approval[k] for k in ('request_id','action_hash','request_scope')},'choice':'y'},host=True)
        return arguments,await bridge.call('exec.start',arguments)

    def evidence(self,name,value):
        # Persist only supplied synthetic facts; never handshake host capability.
        print(json.dumps({'case':name,**value},ensure_ascii=False),file=sys.stderr)

    async def test_real_service_crash_reconnect_unknown_never_relaunches_same_call(self):
        first=await self.bridge('crash-fixture')
        arguments,started=await self.start(first,
            'from pathlib import Path;import time;'
            'p=Path("launch-count");p.write_text(p.read_text()+"x" if p.exists() else "x");'
            'Path("worker-running").write_text("running");time.sleep(2);'
            'Path("worker-terminal").write_text("physical completion")','crash-call')
        self.assertFalse(started['terminal']);self.assertEqual(await self.marker('worker-running'),'running')
        intent=next((self.root/'data').rglob('observation_intent.json'))
        before=hashlib.sha256(intent.read_bytes()).hexdigest()
        old_pid=first.proc.pid;first.proc.kill();await asyncio.wait_for(first.proc.wait(),5)
        self.assertEqual(first.proc.returncode,-9)
        second=await self.bridge('crash-fixture');self.assertNotEqual(second.proc.pid,old_pid)
        observed=await second.call('exec.status',{'run_ref':started['run_ref']})
        replay=await second.call('exec.start',arguments)
        for result in (observed,replay):
            self.assertEqual(result['status'],'reconciliation_unknown')
            self.assertEqual(result['run_ref'],started['run_ref'])
            self.assertFalse(result['owner_available']);self.assertFalse(result['terminal'])
            self.assertEqual(result['error'],'FORMAL_OWNER_LOST_NO_RELAUNCH')
        self.assertEqual(await self.marker('worker-terminal'),'physical completion')
        again=await second.call('exec.start',arguments)
        self.assertEqual(again['status'],'reconciliation_unknown')
        self.assertEqual((self.root/'launch-count').read_text(),'x')
        self.assertEqual(hashlib.sha256(intent.read_bytes()).hexdigest(),before)
        self.assertFalse((await second.call('session.lifecycle'))['active_jobs'])
        self.evidence('P08.e-real-service-crash-L3',{'old_service_exit':first.proc.returncode,
            'old_pid':old_pid,'new_pid':second.proc.pid,'run_ref':started['run_ref'],
            'intent_sha256_unchanged':before,'physical_worker_completed':True,'launch_count':1,
            'status_after_physical_completion':again['status'],'trace':second.trace})

    async def test_cancelled_old_terminal_delivered_after_legal_new_request_retains_scope(self):
        bridge=await self.bridge('late-receipt-fixture')
        original=await bridge.begin()
        _,started=await self.start(bridge,
            'from pathlib import Path;import time;Path("late-worker-running").write_text("running");'
            'time.sleep(3);Path("must-not-finish").write_text("late")','late-call')
        await self.marker('late-worker-running')
        with self.assertRaisesRegex(ReplyError,'ACTIVE_JOB_PREVENTS_NEW_REQUEST'):await bridge.begin('too-early')
        self.assertEqual(bridge.request,'original')
        delayed=await bridge.send('exec.wait',{'run_ref':started['run_ref'],'max_wait_ms':60000})
        await bridge.call('request.cancel',{'reason':'synthetic cancellation'},host=True)
        for _ in range(100):
            if not (await bridge.call('session.lifecycle'))['active_jobs']:break
            await asyncio.sleep(.02)
        else:self.fail('original owner did not settle after cancellation')
        new=await bridge.begin('next-legal-request')
        self.assertNotEqual(original['request_scope'],new['request_scope'])
        # Receiving other IDs buffered this older wait frame; intentionally deliver
        # it to its original waiter only after the new request has been accepted.
        old=await bridge.receive(delayed)
        bridge.trace.append({'event':'delivered_to_original_waiter_after_new_request','id':delayed,
            'accepted_request_id':bridge.request,'run_ref':old.get('run_ref')})
        self.assertTrue(old['terminal']);self.assertTrue(old['cleanup_confirmed'])
        self.assertEqual(old['run_ref'],started['run_ref']);self.assertNotEqual(old.get('exit_code'),0)
        receipt=Path(old['receipt_ref']);saved=receipt.read_bytes();record=json.loads(saved)
        self.assertEqual(record['task_id'],original['task_id'])
        self.assertEqual(record['request_scope'],original['request_scope'])
        for method,params in (
            ('exec.status',{'run_ref':started['run_ref']}),
            ('exec.cancel',{'run_ref':started['run_ref']}),
            ('files.write',{'call_id':'late-write','path':'late-side-effect','content':'must not write'})):
            with self.assertRaisesRegex(ReplyError,'STALE_ACTIVE_REQUEST'):
                await bridge.call(method,params,request='original')
        wrong_scope=await bridge.call('exec.status',{'run_ref':started['run_ref']})
        self.assertEqual(wrong_scope['status'],'not_run')
        self.assertEqual(wrong_scope['error'],'FORMAL_RUN_SCOPE_MISMATCH')
        still_current=await bridge.begin('next-legal-request')
        self.assertEqual(still_current['request_scope'],new['request_scope'])
        self.assertEqual(receipt.read_bytes(),saved)
        self.assertFalse((self.root/'late-side-effect').exists());self.assertFalse((self.root/'must-not-finish').exists())
        self.evidence('P08.c-P16.b-L3-only',{'old_run_ref':started['run_ref'],
            'old_request_scope':original['request_scope'],'new_request_scope':new['request_scope'],
            'old_terminal_receipt_sha256':hashlib.sha256(saved).hexdigest(),'receipt_unchanged':True,
            'wrong_scope_result':wrong_scope,'late_side_effect':False,'trace':bridge.trace})


if __name__=='__main__':unittest.main(verbosity=2)
