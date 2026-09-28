#!/usr/bin/env python3
"""Persistent newline JSON domain service. stdout frames, stderr diagnostics."""
import argparse
import asyncio
from collections import deque
import importlib.abc
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode=True
PROTOCOL_OUT=sys.stdout


class DisconnectTolerantDiagnostics:
    """A closed client's diagnostic pipe cannot cancel an independent job."""
    def __init__(self,stream):self.stream=stream;self.disconnected=False
    def write(self,text):
        if self.disconnected:return len(text)
        try:return self.stream.write(text)
        except (BrokenPipeError,ConnectionResetError):
            self.disconnected=True
            return len(text)
    def flush(self):
        if self.disconnected:return
        try:self.stream.flush()
        except (BrokenPipeError,ConnectionResetError):self.disconnected=True
    def __getattr__(self,name):return getattr(self.stream,name)


# Legacy domain diagnostics use either stdout or stderr. Protocol frames retain
# their separate original fd; only diagnostic consumer disconnects are ignored.
sys.stderr=DisconnectTolerantDiagnostics(sys.stderr)
sys.stdout=sys.stderr
MAX_FRAME=1024*1024
SCOPED_MUTATIONS={'exec.start','exec.cancel','files.write','memory.write','install.propose','install.execute',
                  'approval.decide','request.cancel','memory.maintenance.prepare','memory.maintenance.apply','archive.append'}


class NoLegacyRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='config' or fullname.startswith('config.') or fullname in {
            'bio_agents','agent_core.provider_attempt','agent_core.tool_loop',
            'agent_core.installer','agent_core.memory_maintenance','agent_core.workflow.runtime_graph',
            'agent_core.workflow.pi_rpc','dotenv'}:
            raise ImportError('PI_DOMAIN_FORBIDDEN_IMPORT:'+fullname)


def protect_secret_files(event,args):
    if event=='open' and args and isinstance(args[0],(str,bytes)):
        path=Path(args[0].decode() if isinstance(args[0],bytes) else args[0])
        if path.name.startswith('.env') or path.name in {'auth.json','credentials.json'}:
            raise PermissionError('PI_DOMAIN_SECRET_FILE_FORBIDDEN')


async def serve(service):
    from agent_core.cli_display import safe_text
    write_lock=asyncio.Lock(); pending=set();seen=set();recent=deque();limit=asyncio.Semaphore(64)
    controls=asyncio.Semaphore(64)
    control_methods={'hello','request.begin','request.cancel','approval.decide','exec.cancel',
                     'exec.status','session.lifecycle','context.snapshot','settings.update','shutdown','archive.status'}
    loop=asyncio.get_running_loop()
    reader=asyncio.StreamReader(limit=MAX_FRAME)
    protocol=asyncio.StreamReaderProtocol(reader)
    input_transport,_=await loop.connect_read_pipe(lambda:protocol,sys.stdin.buffer)
    output_fd=PROTOCOL_OUT.fileno()
    os.set_blocking(output_fd,False)
    async def write_frame(frame):
        payload=memoryview((json.dumps(frame,ensure_ascii=False,default=str)+'\n').encode())
        while payload:
            try:payload=payload[os.write(output_fd,payload):]
            except BlockingIOError:
                ready=loop.create_future()
                def writable():
                    if not ready.done():ready.set_result(None)
                loop.add_writer(output_fd,writable)
                try:await ready
                finally:loop.remove_writer(output_fd)
    async def send(frame):
        async with write_lock:
            try:
                await write_frame(frame)
            except (BrokenPipeError,OSError): pass
    async def dispatch(frame):
        identity=frame.get('id')
        async with (controls if frame.get('method') in control_methods else limit):
            try:
                if frame['method'] in SCOPED_MUTATIONS and not frame.get('params',{}).get('active_request_id'):
                    raise ValueError('ACTIVE_REQUEST_ID_REQUIRED')
                result=await service.call(frame['method'],frame.get('params',{}),host_token=frame.get('host_token'))
                reply={'id':identity,'result':result}
            except asyncio.CancelledError:
                reply={'id':identity,'error':{'code':'REQUEST_ABORTED','message':'Transport waiter aborted; independent jobs retain their owner.'}}
            except Exception as exc:
                reply={'id':identity,'error':{'code':type(exc).__name__,'message':safe_text(str(exc))}}
        await send(reply)
    try:
        while not service._closed:
            try:raw=await reader.readline()
            except (BrokenPipeError,ConnectionResetError):break
            except ValueError:
                await send({'id':None,'error':{'code':'PROTOCOL_ERROR','message':'FRAME_TOO_LARGE'}})
                continue
            if not raw: break
            try:
                if len(raw)>MAX_FRAME:raise ValueError('FRAME_TOO_LARGE')
                frame=json.loads(raw)
                if not isinstance(frame,dict) or not isinstance(frame.get('id'),(str,int)) or not isinstance(frame.get('method'),str):raise ValueError('FRAME_INVALID')
                if frame['id'] in seen:raise ValueError('DUPLICATE_TRANSPORT_ID')
                seen.add(frame['id'])
                recent.append(frame['id'])
                if len(recent)>4096:seen.discard(recent.popleft())
                task=asyncio.create_task(dispatch(frame));pending.add(task);task.add_done_callback(pending.discard)
            except Exception as exc:
                await send({'id':None,'error':{'code':'PROTOCOL_ERROR','message':safe_text(str(exc))}})
    finally:
        # Drain on EOF/reset and on unexpected transport failures. Other failures
        # still propagate after owner settlement instead of cancelling jobs via
        # asyncio.run's global task cleanup.
        try:
            if pending:await asyncio.gather(*pending,return_exceptions=True)
            if service.active_jobs():
                print('PI_DOMAIN_CLIENT_DISCONNECTED: independent jobs retain owners until terminal.',file=sys.stderr)
            await service.disconnected()
        finally:input_transport.close()


def main():
    parser=argparse.ArgumentParser()
    for key in ('cwd','data-root','install-root','session-id'):parser.add_argument('--'+key,required=True)
    for key in ('registry-path','environment-inventory','skills-root','preferences-path','memory-root','wiki-root','archive-path'):parser.add_argument('--'+key)
    parser.add_argument('--history-enabled',action='store_true')
    parser.add_argument('--memory-mode',default='ask',choices=('ask','auto','manual','disabled'))
    options=vars(parser.parse_args())
    sys.path.insert(0,str(Path(options['install_root']).resolve()))
    sys.meta_path.insert(0,NoLegacyRuntime());sys.addaudithook(protect_secret_files)
    from domain_adapter.service import DomainService
    asyncio.run(serve(DomainService(**options)))


if __name__=='__main__':main()
