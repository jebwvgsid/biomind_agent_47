"""Client pipe closure must not revoke an already approved formal job owner."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

from test_python_lifecycle import ProductBridge


class DisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_diagnostics_and_protocol_pipes_preserve_original_owner(self):
        with tempfile.TemporaryDirectory(prefix='biomind-closed-pipes-') as folder:
            root=Path(folder)
            (root/'registry.json').write_text('{"environments":{},"tools":{}}')
            (root/'preferences.md').write_text('Synthetic disconnect fixture')
            bridge=await ProductBridge(root,'closed-pipes-fixture').open()
            try:
                await bridge.begin()
                code=('from pathlib import Path;import os,time,json;'
                      'Path("started.json").write_text(json.dumps({"pid":os.getpid(),"ppid":os.getppid()}));'
                      'print("before disconnect",flush=True);'
                      '\nwhile not Path("release").exists():time.sleep(.02)\n'
                      'Path("finished.json").write_text(json.dumps({"pid":os.getpid(),"ppid":os.getppid()}));'
                      'print("after disconnect",flush=True)')
                args={'call_id':'closed-pipes','arguments':{'argv':[sys.executable,'-I','-u','-c',code]}}
                pending=await bridge.call('exec.start',args);approval=pending['approval']
                await bridge.call('approval.decide',{**{k:approval[k] for k in ('request_id','action_hash','request_scope')},'choice':'y'},host=True)
                started=await bridge.call('exec.start',args)
                for _ in range(250):
                    if (root/'started.json').exists():break
                    await asyncio.sleep(.02)
                self.assertTrue((root/'started.json').is_file())
                identity=json.loads((root/'started.json').read_text());owner_pid=bridge.proc.pid
                # Model native parent exit: stop reading both child output pipes,
                # then close input. There is no cancellation/domain shutdown call.
                bridge.proc.stderr._transport.close()
                bridge.proc.stdout._transport.close()
                bridge.proc.stdin.close()
                await asyncio.sleep(.2)
                still_owned=bridge.proc.returncode is None
                (root/'release').write_text('natural completion authorized by fixture')
                # Polling also captures child exit independently of a closed
                # asyncio pipe's wait notification on this execution host.
                for _ in range(500):
                    if bridge.proc.returncode is not None:break
                    await asyncio.sleep(.02)
                receipts=list((root/'data').rglob('exit_status.json'))
                receipt=json.loads(receipts[0].read_text()) if receipts else {}
                finished=json.loads((root/'finished.json').read_text()) if (root/'finished.json').exists() else None
                print(json.dumps({'case':'closed-client-pipes-real-formal-owner','service_pid':owner_pid,
                    'service_alive_before_release':still_owned,'service_exit':bridge.proc.returncode,
                    'worker_started':identity,'worker_finished':finished,'run_ref':started['run_ref'],
                    'receipt_exit_code':receipt.get('exit_code'),'receipt_status':receipt.get('status'),
                    'cancellation_source':receipt.get('runtime_metadata',{}).get('cancellation_source')}),file=sys.stderr)
                self.assertTrue(still_owned,'domain owner exited when diagnostics consumer disconnected')
                self.assertEqual(bridge.proc.returncode,0)
                self.assertEqual(finished,identity)
                self.assertEqual(receipt.get('exit_code'),0)
                self.assertNotEqual(receipt.get('status'),'cancelled')
            finally:
                (root/'release').touch()
                if bridge.proc.returncode is None:
                    bridge.proc.stdin.close()
                    try:await asyncio.wait_for(bridge.proc.wait(),10)
                    except asyncio.TimeoutError:bridge.proc.kill();await bridge.proc.wait()


if __name__=='__main__':unittest.main(verbosity=2)
