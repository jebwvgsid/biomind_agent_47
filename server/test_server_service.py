"""Lifecycle regression tests. Fake scheduler/health only; no model or socket."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).with_name('server-service.py')
SPEC = importlib.util.spec_from_file_location('server_service', SCRIPT)
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)
PREFLIGHT_SPEC=importlib.util.spec_from_file_location('service_preflight',SCRIPT.with_name('preflight.py'))
preflight=importlib.util.module_from_spec(PREFLIGHT_SPEC)
PREFLIGHT_SPEC.loader.exec_module(preflight)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='biomind-service-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'config with spaces.json'
        self.sbatch = self.root / 'fake-sbatch'
        self.squeue = self.root / 'fake-squeue'
        self.sbatch.write_text('#!' + sys.executable + '\n' +
            'import json,pathlib,sys\n' +
            'root=pathlib.Path(__file__).parent\n' +
            'name=next(x.split("=",1)[1] for x in sys.argv if x.startswith("--job-name="))\n' +
            'with (root/"submissions").open("a") as f:f.write(json.dumps(sys.argv)+"\\n")\n' +
            '(root/"queue").write_text("777|PENDING|(null)|"+name+"\\n")\n' +
            'print("777")\n')
        self.squeue.write_text('#!' + sys.executable + '\n' +
            'import pathlib,sys\n' +
            'root=pathlib.Path(__file__).parent\n' +
            'if (root/"scheduler-unavailable").exists():sys.exit(1)\n' +
            'print((root/"queue").read_text() if (root/"queue").exists() else "",end="")\n')
        self.sbatch.chmod(0o700)
        self.squeue.chmod(0o700)
        self.value = dict(schema=service.SCHEMA, serviceId='test-service', serverId='biomind-test', mode='slurm',
            stateRoot=str(self.root / 'state'), cwd=str(self.root), command=[sys.executable, '-c', 'pass'],
            slurm=dict(sbatch=str(self.sbatch), squeue=str(self.squeue), cpus=2, memory='1G', time='00:10:00'))
        self.save_config()

    def save_config(self):
        self.config_path.write_text(json.dumps(self.value))
        self.config = service.load(self.config_path)

    def state(self):
        return service.read_record(self.config)

    def submissions(self):
        path = self.root / 'submissions'
        return path.read_text().splitlines() if path.exists() else []

    def queued_to_running(self):
        state = self.state()
        (self.root / 'queue').write_text('777|RUNNING|compute-new|' + state['jobName'] + '\n')
        return state

    def test_concurrent_ensure_submits_once_and_returns_same_job(self):
        children = [subprocess.Popen([sys.executable, str(SCRIPT), 'ensure', '--config', str(self.config_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(6)]
        results = []
        for child in children:
            out, err = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, err)
            results.append(json.loads(out))
        self.assertEqual(len(self.submissions()), 1)
        self.assertEqual({result['jobId'] for result in results}, {'777'})
        self.assertEqual({result['status'] for result in results}, {'pending'})
        self.assertEqual(sum(not result['reused'] for result in results), 1)
        self.assertTrue(all(result['transport'] == 'jump' for result in results))

    def test_submission_wrap_quotes_paths_and_never_hardcodes_endpoint(self):
        service.ensure(self.config)
        command = json.loads(self.submissions()[0])
        wrap = next(item for item in command if item.startswith('--wrap='))
        import shlex
        parsed = shlex.split(wrap[len('--wrap=exec '):])
        self.assertIn(str(self.config_path), parsed)
        self.assertNotIn('--nodelist', ' '.join(command))
        self.assertIn('--cpus-per-task=2', command)
        self.assertIn('--mem=1G', command)

    def test_stale_ready_never_exposed_as_ready(self):
        service.ensure(self.config)
        row = self.queued_to_running()
        row.update(status='ready', host='compute-new', port=30279, heartbeatAt=time.time()-30)
        service.write_record(self.config, row)
        result = service.discover(self.config)
        self.assertEqual(result['status'], 'starting')
        self.assertEqual(result['code'], 'HEALTH_CONFIRMATION_PENDING')
        row['heartbeatAt'] = time.time()
        service.write_record(self.config, row)
        result = service.discover(self.config)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['host'], 'compute-new')
        self.assertEqual(result['port'], 30279)

    def test_scheduler_failure_does_not_submit_replacement(self):
        service.ensure(self.config)
        (self.root / 'scheduler-unavailable').touch()
        result = service.ensure(self.config)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(len(self.submissions()), 1)

    def test_scheduler_job_identity_mismatch_blocks_attach_and_restart(self):
        service.ensure(self.config)
        (self.root / 'queue').write_text('777|RUNNING|compute-other|unrelated-job\n')
        result = service.ensure(self.config)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['code'], 'JOB_IDENTITY_MISMATCH')
        self.assertEqual(len(self.submissions()), 1)

    def test_submission_timeout_persists_intent_and_never_replays(self):
        with patch.object(service.subprocess, 'run', side_effect=subprocess.TimeoutExpired('sbatch', 20)):
            result = service.ensure(self.config)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.state()['status'], 'submission-unknown')
        # No scheduler evidence yet: repeated launch remains query-only.
        with patch.object(service, 'queue', return_value=None), patch.object(service.subprocess, 'Popen') as spawn:
            result = service.ensure(self.config)
        self.assertEqual(result['status'], 'unknown')
        spawn.assert_not_called()
        self.assertEqual(self.submissions(), [])

    def test_uncertain_submission_is_discovered_by_unique_name(self):
        with patch.object(service.subprocess, 'run', side_effect=subprocess.TimeoutExpired('sbatch', 20)):
            service.ensure(self.config)
        row = self.state()
        (self.root / 'queue').write_text('991|PENDING|(null)|' + row['jobName'] + '\n')
        result = service.ensure(self.config)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['jobId'], '991')
        self.assertTrue(result['reused'])
        self.assertEqual(self.submissions(), [])

    def test_finished_job_allows_one_new_launch(self):
        first = service.ensure(self.config)
        (self.root / 'queue').write_text('')
        second = service.ensure(self.config)
        self.assertNotEqual(first['instanceId'], second['instanceId'])
        self.assertEqual(len(self.submissions()), 2)

    def test_active_config_change_does_not_replace_job(self):
        service.ensure(self.config)
        self.value['slurm']['cpus'] = 3
        self.save_config()
        result = service.ensure(self.config)
        self.assertEqual(result['status'], 'configuration-changed')
        self.assertEqual(len(self.submissions()), 1)

    def test_health_requires_current_instance_and_loopback(self):
        instance = 'c'*32
        endpoint = dict(schema='BioMindStudioEndpointV1',instanceId=instance,serverId='biomind-test',
                        address='127.0.0.1',port=23456)
        path = Path(self.config['stateRoot']) / (instance + '.endpoint.json')
        service.atomic(path, endpoint)
        response = Mock()
        response.status = 200
        response.read.return_value = json.dumps(dict(schema='BioMindStudioHealthV1',instanceId='wrong',
            serverId='biomind-test',profile='biomind',status='ready')).encode()
        manager = Mock()
        manager.__enter__ = Mock(return_value=response)
        manager.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = manager
        with patch.object(service.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(service.Refused, 'HEALTH_IDENTITY_MISMATCH'):
                service.health(self.config, instance)
        endpoint['address'] = '0.0.0.0'
        service.atomic(path, endpoint)
        with self.assertRaisesRegex(service.Refused, 'ENDPOINT_IDENTITY_MISMATCH'):
            service.health(self.config, instance)

    def test_unready_health_is_not_successful_socket_probe(self):
        self.assertIsNone(service.health(self.config, 'd'*32))

    def test_private_state_and_environment_file_are_required(self):
        private_file = self.root / 'environment.json'
        private_file.write_text('{}')
        private_file.chmod(0o644)
        self.value['environmentFile'] = str(private_file)
        with self.assertRaisesRegex(service.Refused, 'PRIVATE_STATE_PERMISSIONS_REQUIRED'):
            self.save_config()

    def test_existing_main_paths_preserved_and_no_fixture_imposed(self):
        self.value['environment'] = dict(BIOMIND_RUNTIME_CONFIG='/existing/runtime.json',
            BIOMIND_SESSION_ROOT='/existing/sessions', BIOMIND_PI_SKILLS_ROOT='/existing/skills')
        self.save_config()
        with patch.dict(os.environ, {}, clear=True):
            env = service.environment(self.config, 'e'*32)
        self.assertEqual(env['BIOMIND_SESSION_ROOT'], '/existing/sessions')
        self.assertEqual(env['BIOMIND_RUNTIME_CONFIG'], '/existing/runtime.json')
        self.assertEqual(env['PI_STUDIO_AGENT_PROFILE'], 'biomind')
        self.assertNotIn('PI_OFFLINE', env)
        self.assertNotIn('HOME', env)
        self.assertNotIn('BIOMIND_DISABLE_DOTENV', env)

    def test_preflight_never_reads_credential_file_or_claims_runtime_pass(self):
        environment_file=self.root/'private-environment.json'
        environment_file.write_text('{"PRIVATE_TEST":"DO_NOT_READ"}')
        environment_file.chmod(0o600)
        self.value['environmentFile']=str(environment_file)
        self.save_config()
        reads=[]
        original=Path.read_text
        def read(path,*args,**kwargs):
            reads.append(path)
            self.assertNotEqual(path,environment_file)
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',read):
            result=preflight.inspect(self.config_path)
        self.assertEqual(reads,[self.config_path])
        self.assertFalse(result['credentialsRead'])
        self.assertFalse(result['networkUsed'])
        self.assertFalse(result['serviceStarted'])
        checks={row['check']:row['result'] for row in result['checks']}
        self.assertEqual(checks['credential_file_permissions_only'],'PASS')
        self.assertEqual(checks['model_initialization'],'NOT_RUN')
        self.assertEqual(checks['windows_application'],'NOT_RUN')

    def test_run_rejects_second_holder_before_spawning_backend(self):
        self.value['mode'] = 'local'
        self.save_config()
        row = dict(schema=service.SCHEMA, serviceId=self.config['serviceId'],serverId=self.config['serverId'],
            binding=self.config['binding'],instanceId='a'*32,holderStarted=True)
        service.write_record(self.config, row)
        with patch.object(service.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(service.Refused, 'SERVICE_HOLDER_ALREADY_STARTED'):
                service.run(self.config, 'a'*32)
        spawn.assert_not_called()

    def test_lost_holder_with_surviving_backend_never_starts_replacement(self):
        self.value['mode']='local'
        self.save_config()
        row=dict(schema=service.SCHEMA,serviceId=self.config['serviceId'],serverId=self.config['serverId'],
            binding=self.config['binding'],instanceId='f'*32,status='ready',host='departed-host',pid=1,startTicks='unknown')
        service.write_record(self.config,row)
        fd=service.backend_lease(self.config)
        try:
            with patch.object(service.subprocess,'Popen') as spawn:
                result=service.ensure(self.config)
            self.assertEqual(result['status'],'unknown')
            self.assertEqual(result['code'],'EXISTING_BACKEND_LIFETIME_LOCKED')
            spawn.assert_not_called()
        finally:
            os.close(fd)

    def test_slow_initialization_remains_same_instance_not_ready(self):
        self.value['mode'] = 'local'
        self.value['startupTimeoutSeconds'] = 5
        self.save_config()
        row = dict(schema=service.SCHEMA,serviceId=self.config['serviceId'],serverId=self.config['serverId'],
            binding=self.config['binding'],instanceId='b'*32)
        service.write_record(self.config,row)
        process = Mock()
        process.poll.side_effect = [None,None,0]
        process.returncode = 0
        records = []
        real_write = service.write_record
        def capture(config, record):
            records.append(dict(record))
            real_write(config,record)
        with patch.object(service.subprocess,'Popen',return_value=process) as spawn, \
             patch.object(service,'health',return_value=None), \
             patch.object(service.time,'sleep'), \
             patch.object(service.time,'monotonic',side_effect=[0,0,10,10,10,10,10,10]), \
             patch.object(service.signal,'signal'), \
             patch.object(service,'write_record',side_effect=capture):
            service.run(self.config,'b'*32)
        self.assertEqual(spawn.call_count,1)
        self.assertIn('slow-start',[item['status'] for item in records])
        self.assertNotIn('ready',[item['status'] for item in records])
        self.assertEqual({item['instanceId'] for item in records},{'b'*32})


if __name__ == '__main__':
    unittest.main(verbosity=2)
