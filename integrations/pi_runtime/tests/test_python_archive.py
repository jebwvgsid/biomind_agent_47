"""Original optional SessionArchive writer, synthetic stores, actual STDIO EOF."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

# Reuse the no-config/no-old-agent import guard; no old sessions are opened.
from test_python_domain import DomainService,DomainError,REPO,RUNTIME


class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='biomind-pi-archive-')
        self.root=Path(self.temp.name);self.services=[]
        self.archive=self.root/'explicit-archive.sqlite3'
        self.registry=self.root/'registry.json';self.registry.write_text('{"environments":{},"tools":{}}')
        self.preferences=self.root/'preferences.md';self.preferences.write_text('Synthetic preference')

    async def service(self,enabled=True):
        value=DomainService(cwd=self.root,data_root=self.root/'data',install_root=REPO,session_id='archive-fixture',
            registry_path=self.registry,preferences_path=self.preferences,skills_root=self.root/'skills',
            memory_root=self.root/'memory',history_enabled=False,archive_path=self.archive if enabled else None)
        self.services.append(value)
        await value.call('request.begin',{'request_id':'fixture','role':'consult'},host_token=value.host_token)
        return value

    async def asyncTearDown(self):
        for service in self.services:await service.disconnected()
        self.temp.cleanup()

    def event(self,event_id='pi:archive-fixture:entry-1',**extra):
        return {'active_request_id':'fixture','event_id':event_id,'event_type':'assistant_message',
            'stage':'consult','content':'Same public answer','source_refs':[event_id],
            'metadata':{'pi_entry_id':event_id.rsplit(':',1)[-1]},**extra}

    async def append(self,service,**extra):
        return await service.call('archive.append',self.event(**extra),host_token=service.host_token)

    def rows(self):
        with closing(sqlite3.connect(self.archive)) as conn:
            conn.row_factory=sqlite3.Row
            return [dict(row) for row in conn.execute('SELECT * FROM session_events ORDER BY id')]

    async def test_exact_duplicate_restart_and_distinct_event_with_history_read_disabled(self):
        service=await self.service()
        self.assertTrue(service.context_snapshot()['archive_enabled'])
        first=await self.append(service);again=await self.append(service)
        self.assertIn(first['status'],{'enqueued','coalesced'})
        self.assertEqual(first['logical_event_id'],again['logical_event_id'])
        self.assertFalse(first['durable'])
        await service.disconnected()
        self.assertEqual(len(self.rows()),1)
        reconnected=await self.service()
        replay=await self.append(reconnected)
        distinct=await self.append(reconnected,event_id='pi:archive-fixture:entry-2')
        self.assertEqual(first['logical_event_id'],replay['logical_event_id'])
        self.assertNotEqual(first['logical_event_id'],distinct['logical_event_id'])
        await reconnected.disconnected()
        rows=self.rows();self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['content'],rows[1]['content'])
        self.assertEqual(json.loads(rows[0]['source_refs_json']),['pi:archive-fixture:entry-1'])
        self.assertEqual(rows[0]['task_id'],service.task_id)
        self.assertEqual(rows[0]['session_id'],service.session_id)
        for method in ('memory.search','memory.read'):
            result=await reconnected.call(method,{'source':'archive','source_ref':'session_archive:1'})
            self.assertEqual(result['status'],'disabled')
        stats=(await reconnected.call('archive.status',{},host_token=reconnected.host_token))['stats']
        self.assertEqual(stats['write_failure_count'],0)
        self.assertEqual(stats['remaining_not_durable_count'],0)
        self.assertFalse((self.root/'memory').exists())

    async def test_no_explicit_archive_path_means_no_archive_io(self):
        service=await self.service(enabled=False)
        before={str(path.relative_to(self.root)) for path in self.root.rglob('*')}
        self.assertFalse(service.context_snapshot()['archive_enabled'])
        self.assertEqual((await self.append(service))['status'],'disabled')
        self.assertEqual((await service.call('archive.status',{},host_token=service.host_token))['status'],'disabled')
        await service.disconnected()
        self.assertIsNone(service._archive_writer)
        self.assertFalse(self.archive.exists())
        self.assertEqual(before,{str(path.relative_to(self.root)) for path in self.root.rglob('*')})

    async def test_host_scope_public_projection_limits_and_metadata_only_event(self):
        service=await self.service()
        with self.assertRaisesRegex(DomainError,'HOST_CHANNEL_REQUIRED'):
            await service.call('archive.append',self.event())
        with self.assertRaisesRegex(DomainError,'HOST_CHANNEL_REQUIRED'):
            await service.call('archive.status',{})
        with self.assertRaisesRegex(DomainError,'STALE_ACTIVE_REQUEST'):
            await self.append(service,active_request_id='obsolete')
        with self.assertRaisesRegex(DomainError,'ACTIVE_REQUEST_ID_REQUIRED'):
            await self.append(service,active_request_id=None)
        with self.assertRaisesRegex(DomainError,'ARCHIVE_PUBLIC_EVENT_TYPE_REQUIRED'):
            await self.append(service,event_type='thinking')
        self.assertFalse(self.archive.exists())
        await self.append(service,event_type='tool_result',content='x'*64001,summary='s'*1201,
            task_id='spoof',session_id='spoof',metadata={'approved':True,'source':'human',
                'thinking':'private fixture excluded','pi_request_id':'spoof','pi_entry_id':'entry-1',
                'tool_name':'run_command','tool_call_id':'call-1','is_error':False})
        await self.append(service,event_id='pi:archive-fixture:empty',content='')
        await service.disconnected()
        first,empty=self.rows();metadata=json.loads(first['metadata_json'])
        self.assertEqual(metadata['pi_request_id'],'fixture')
        self.assertEqual(metadata['tool_call_id'],'call-1')
        self.assertTrue(metadata['projection_truncated'])
        for field in ('approved','source','thinking'):self.assertNotIn(field,metadata)
        self.assertEqual(first['task_id'],service.task_id)
        refs=json.loads(first['large_refs_json'])
        content_ref=next(row for row in refs if row['key_path']=='content')
        self.assertEqual(content_ref['chars'],64000)
        self.assertEqual(Path(content_ref['path']).read_text(),'x'*64000)
        self.assertEqual(len(first['summary']),1200)
        self.assertEqual(empty['event_type'],'assistant_message')
        self.assertEqual(json.loads(empty['metadata_json'])['event_id'],'pi:archive-fixture:empty')
        # Preserve SessionArchive's existing content-or-stage fallback.
        self.assertEqual(empty['content'],'consult')

    async def test_actual_product_stdio_eof_drains_original_writer_without_jobs(self):
        proc=await asyncio.create_subprocess_exec(sys.executable,'-B','-u',str(RUNTIME/'python_service.py'),
            '--cwd',str(self.root),'--data-root',str(self.root/'stdio-data'),'--install-root',str(REPO),
            '--session-id','stdio-archive-fixture','--registry-path',str(self.registry),
            '--preferences-path',str(self.preferences),'--archive-path',str(self.archive),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
            env={'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin','LC_ALL':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
        async def call(identity,method,params=None,token=None):
            proc.stdin.write((json.dumps({'id':identity,'method':method,'params':params or {},'host_token':token})+'\n').encode())
            await proc.stdin.drain()
            reply=json.loads(await asyncio.wait_for(proc.stdout.readline(),10))
            self.assertEqual(reply['id'],identity);self.assertNotIn('error',reply)
            return reply['result']
        try:
            hello=await call('hello','hello');token=hello['host_token']
            await call('begin','request.begin',{'request_id':'fixture'},token)
            snapshot=await call('context','context.snapshot')
            self.assertTrue(snapshot['archive_enabled']);self.assertFalse(snapshot['history_enabled'])
            self.assertFalse((await call('jobs','session.lifecycle'))['active_jobs'])
            # Queue frames then close stdin immediately, without consuming ACKs.
            for number in range(12):
                frame={'id':'event-'+str(number),'method':'archive.append','params':self.event(
                    event_id='pi:stdio-archive-fixture:'+str(number)),'host_token':token}
                proc.stdin.write((json.dumps(frame)+'\n').encode())
            await proc.stdin.drain();proc.stdin.close()
            stdout,stderr=await asyncio.wait_for(proc.communicate(),10)
            self.assertEqual(proc.returncode,0,stderr.decode(errors='replace'))
            replies=[json.loads(line) for line in stdout.splitlines()]
            self.assertEqual(len(replies),12)
            for reply in replies:
                self.assertNotIn('error',reply)
                self.assertIn(reply['result']['status'],{'enqueued','coalesced'})
                self.assertFalse(reply['result']['durable'])
            self.assertEqual(len(self.rows()),12)
            print('archive EOF: 12 actual SQLite events, history off, no active jobs, rc=0',file=sys.stderr)
        finally:
            if proc.returncode is None:proc.kill();await proc.wait()


if __name__=='__main__':unittest.main(verbosity=2)
