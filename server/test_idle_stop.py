"""Real isolated service holder + HTTP idle stop, and refusal regression cases."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('idle_service',Path(__file__).with_name('server-service.py'))
service=importlib.util.module_from_spec(spec);spec.loader.exec_module(service)

class IdleStopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='biomind-idle-stop-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.file=self.root/'service.json'
        self.raw=dict(schema=service.SCHEMA,serviceId='idle-test',serverId='biomind-idle-test',mode='local',stateRoot=str(self.root/'state'),cwd=str(self.root),command=[sys.executable,'-c','pass'])
        self.file.write_text(json.dumps(self.raw));self.config=service.load(self.file)
    def record(self):
        value=dict(schema=service.SCHEMA,serviceId=self.config['serviceId'],serverId=self.config['serverId'],instanceId='a'*32,host=socket.gethostname(),binding=self.config['binding'],status='ready',pid=os.getpid(),startTicks=service.ticks(os.getpid()))
        service.write_record(self.config,value);return value
    def test_missing_or_wrong_instance_never_creates_stopped_record(self):
        with self.assertRaisesRegex(service.Refused,'SERVICE_INSTANCE_REPLACED'):service.stop(self.config,'a'*32)
        self.assertIsNone(service.read_record(self.config));self.record()
        with self.assertRaisesRegex(service.Refused,'SERVICE_INSTANCE_REPLACED'):service.stop(self.config,'b'*32)
        self.assertEqual(service.read_record(self.config)['status'],'ready')
    def test_foreign_host_and_old_protocol_do_not_send_requests_or_signals(self):
        row=self.record();row['host']='foreign';service.write_record(self.config,row)
        with self.assertRaisesRegex(service.Refused,'RUN_ON_SERVICE_HOST_REQUIRED'):service.stop(self.config,'a'*32)
        self.record()
        with patch.object(service,'health',return_value={'port':1234}), patch.object(service,'environment') as secret:
            with self.assertRaisesRegex(service.Refused,'IDLE_STOP_PROTOCOL_UNAVAILABLE'):service.stop(self.config,'a'*32)
            secret.assert_not_called()
        self.assertEqual(service.read_record(self.config)['status'],'ready')
    def test_foreign_host_is_unknown_and_cannot_start_replacement(self):
        row=self.record();row['host']='foreign';service.write_record(self.config,row)
        with patch.object(service.subprocess,'Popen') as spawn:
            self.assertEqual(service.ensure(self.config)['status'],'unknown');spawn.assert_not_called()

    def test_real_holder_stop_record_with_simulated_http_transport(self):
        # Socket policy must not be mistaken for a lifecycle failure. Only the
        # HTTP health/POST transport is simulated; holder/child exit and flock
        # release are real. The live socket case remains a separate test.
        trigger=self.root/'stop-fixture'
        self.raw['command']=[sys.executable,'-c',
            'from pathlib import Path;import time,sys;p=Path(sys.argv[1]);end=time.monotonic()+30;\nwhile not p.exists() and time.monotonic()<end:time.sleep(.05)\nsys.exit(0 if p.exists() else 1)',str(trigger)]
        self.file.write_text(json.dumps(self.raw));self.config=service.load(self.file)
        holder=self.root/'holder.py';real=Path(service.__file__).resolve()
        holder.write_text('import importlib.util\ns=importlib.util.spec_from_file_location("service",'+repr(str(real))+');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\nm.health=lambda c,i:{"port":1234,"idleStopProtocol":1}\nraise SystemExit(m.main())\n')
        with patch.object(service,'__file__',str(holder)):started=service.ensure(self.config)
        instance=started['instanceId'];deadline=time.monotonic()+10
        while service.discover(self.config)['status']!='ready' and time.monotonic()<deadline:time.sleep(.05)
        self.assertEqual(service.discover(self.config)['status'],'ready')
        from unittest.mock import Mock
        response=Mock();response.status=200;response.read.return_value=json.dumps(dict(schema='BioMindIdleStopV1',serverId=self.config['serverId'],instanceId=instance,status='stopping')).encode()
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        def request(req,**kwargs):
            self.assertEqual(req.get_method(),'POST');self.assertEqual(req.get_header('X-biomind-instance'),instance)
            self.assertEqual(req.get_header('Authorization'),'Basic cGk6Zml4dHVyZQ==')
            trigger.touch();return response
        opener=Mock();opener.open.side_effect=request
        with patch.object(service,'health',return_value={'port':1234,'idleStopProtocol':1}), patch.object(service,'environment',return_value={'PI_WEB_PASSWORD':'fixture'}), patch.object(service.urllib.request,'build_opener',return_value=opener):
            result=service.stop(self.config,instance,timeout=10)
        row=service.read_record(self.config);self.assertEqual(row['status'],'stopped');self.assertEqual(row['exitCode'],0)
        self.assertFalse(service.backend_lease_held(self.config));self.assertFalse(result['signalsSent'])

    @unittest.skipUnless(os.getenv('BIOMIND_TEST_NODE'),'Explicit isolated Node executable required')
    def test_real_holder_http_shutdown_writes_genuine_stopped_and_releases_lease(self):
        node=os.environ['BIOMIND_TEST_NODE']; entry=self.root/'fixture.mjs'
        launcher=Path(__file__).with_name('next-service.mjs').resolve().as_uri()
        entry.write_text("import {runService} from "+json.dumps(launcher)+";\nawait runService({loadNext:()=>()=>({prepare:async()=>{},getRequestHandler:()=>((req,res)=>{res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({profile:'biomind',serverId:process.env.BIOMIND_SERVER_ID,allowedProjects:[process.cwd()]}));}),close:async()=>{}})});\n")
        self.raw.update(command=[node,str(entry)],environment={'BIOMIND_STUDIO_ROOT':str(self.root),'PI_WEB_PASSWORD':'ISOLATED_TEST_ONLY'})
        self.file.write_text(json.dumps(self.raw));self.config=service.load(self.file)
        started=service.ensure(self.config);instance=started['instanceId']
        def cleanup():
            row=service.read_record(self.config)
            if row and row.get('instanceId')==instance and service.process_alive(row):os.kill(row['pid'],signal.SIGTERM)
        self.addCleanup(cleanup)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            discovered=service.discover(self.config)
            if discovered['status']=='ready':break
            if discovered['status']=='failed':self.fail('Isolated backend failed to start')
            time.sleep(.1)
        if discovered['status'] != 'ready':
            print('ISOLATED_STATE', service.read_record(self.config))
            for log in (self.root/'state/logs').glob('*.log'):print(log.name,log.read_text()[-5000:])
        self.assertEqual(discovered['status'],'ready')
        result=service.stop(self.config,instance,timeout=10)
        self.assertEqual(result['status'],'stopped');self.assertFalse(result['signalsSent'])
        row=service.read_record(self.config);self.assertEqual(row['code'],'BACKEND_EXITED');self.assertEqual(row['exitCode'],0)
        self.assertFalse(service.backend_lease_held(self.config));self.assertFalse(service.process_alive(row))
        print(json.dumps({'classification':'ISOLATED_REAL_LIFECYCLE_WITH_PROFILE_FIXTURE','status':'PASS','instanceId':instance,'exitCode':row['exitCode'],'modelCalls':0}))

if __name__=='__main__':unittest.main()
