import fcntl
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT=Path(__file__).with_name('configure-service.py')
SPEC=importlib.util.spec_from_file_location('configure_service',SCRIPT)
configure=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='biomind-configure-test-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.backend=self.root/'backend';self.main=self.root/'main'
        for base,names in ((self.backend,('package.json','lib/biomind-session-adapter.ts','.next-pkg/BUILD_ID',
                '.next-pkg/server/app/api/agent/profile/route.js','.next-pkg/server/app/api/agent/new/route.js',
                '.next-pkg/server/app/api/agent/[id]/route.js','.next-pkg/server/app/api/agent/[id]/events/route.js')),
                (self.main,('bio_agents.py','integrations/pi_runtime/dist/host.js','integrations/pi_runtime/dist/history-query.js',
                'integrations/pi_runtime/dist/instructions-query.js','integrations/pi_runtime/testing/posix-pipe-relay.py'))):
            for name in names:
                path=base/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('fixture')
        (self.backend/'lib/biomind-session-adapter.ts').write_text('liveSnapshot() displayPendingUI // unit fixture only')
        self.sessions=self.root/'existing-sessions';self.sessions.mkdir()
        self.runtime=self.root/'runtime.json';self.runtime.write_text(json.dumps({'sessionDir':str(self.sessions)}))
        self.project=self.root/'project';self.project.mkdir()
        self.data=self.root/'data';self.data.mkdir()
        self.config=self.root/'private'/'service.json'
        self.password='only-workbench-password-fixture'
        self.setup=dict(schema='BioMindStudioConfigureV1',backendRoot=str(self.backend),mainRoot=str(self.main),
            runtimeConfig=str(self.runtime),sessionRoot=str(self.sessions),pythonPath=sys.executable,nodePath=sys.executable,
            projectRoots=[str(self.project)],dataRoots=[str(self.data)],uploadsRoot=str(self.data),mode='slurm',
            slurm={'cpus':4},workbenchPassword=self.password)

    def test_configure_preserves_native_paths_and_opens_no_referenced_credentials(self):
        opened=[];original=Path.read_text
        def read(path,*args,**kwargs):
            opened.append(path)
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',read):
            result=configure.configure(self.config,self.setup)
        self.assertEqual(result['status'],'configured')
        value=json.loads(self.config.read_text())
        secret=json.loads((self.config.parent/'runtime-environment.json').read_text())
        self.assertEqual(secret,{'PI_WEB_PASSWORD':self.password})
        self.assertNotIn(self.password,self.config.read_text())
        self.assertEqual(value['environment']['BIOMIND_SESSION_ROOT'],str(self.sessions))
        self.assertEqual(value['environment']['BIOMIND_RUNTIME_CONFIG'],str(self.runtime))
        self.assertEqual(json.loads(self.runtime.read_text()),{'sessionDir':str(self.sessions)})
        self.assertFalse(result['apiCredentialsRead']);self.assertFalse(result['modelStarted'])
        self.assertEqual(self.config.stat().st_mode&0o777,0o600)
        self.assertEqual(self.config.parent.stat().st_mode&0o777,0o700)

    def test_runtime_session_scope_must_match_existing_native_root(self):
        other=self.root/'other-sessions';other.mkdir()
        with self.assertRaisesRegex(configure.Refused,'RUNTIME_SESSION_ROOT_MISMATCH'):
            configure.configure(self.config,{**self.setup,'sessionRoot':str(other)})
        self.assertFalse(self.config.exists())

    def test_native_default_keeps_existing_project_session_directories(self):
        agent=self.root/'existing-native-agent';agent.mkdir()
        sessions=agent/'sessions';sessions.mkdir()
        project=sessions/'--original-project--';project.mkdir()
        history=project/'original.jsonl';history.write_text('existing native history\n')
        self.runtime.write_text(json.dumps({'agentDir':str(agent),'dataRoot':'.biomind/pi-domain'}))
        result=configure.configure(self.config,{**self.setup,'sessionRoot':str(sessions)})
        self.assertEqual(result['sessionScope'],'native_project_directories')
        self.assertEqual(history.read_text(),'existing native history\n')
        self.assertNotIn('sessionDir',json.loads(self.runtime.read_text()))
        self.assertEqual(json.loads(self.config.read_text())['environment']['BIOMIND_SESSION_ROOT'],str(sessions))
        self.assertEqual(list(agent.iterdir()),[sessions])

    def test_native_default_requires_explicit_agent_dir_and_matching_existing_root(self):
        self.runtime.write_text('{}')
        with self.assertRaisesRegex(configure.Refused,'RUNTIME_AGENT_DIR_REQUIRED_FOR_NATIVE_SESSIONS'):
            configure.configure(self.config,self.setup)
        agent=self.root/'agent';agent.mkdir();(agent/'sessions').mkdir()
        self.runtime.write_text(json.dumps({'agentDir':str(agent)}))
        with self.assertRaisesRegex(configure.Refused,'RUNTIME_SESSION_ROOT_MISMATCH'):
            configure.configure(self.config,self.setup)
        self.assertFalse(self.config.exists())

    def test_nonsecret_model_record_is_referenced_without_copying_credentials(self):
        self.runtime.write_text(json.dumps({'sessionDir':str(self.sessions),'provider':'deepseek',
            'model':'deepseek-v4-flash','thinkingLevel':'off'}))
        record=self.root/'model-selection.json'
        value=dict(schema='biomind-active-model-config-v2',provider='deepseek',model='deepseek-v4-flash',
            api='openai-completions',thinking='off',endpoint_hostname='api.deepseek.com',
            origin='active-launch-environment',confirmed_active=True,endpoint_base_url='https://api.deepseek.com')
        record.write_text(json.dumps(value))
        result=configure.configure(self.config,{**self.setup,'modelRecordPath':str(record)})
        self.assertEqual(result['checks']['modelRecordMetadata'],'PASS')
        self.assertEqual(json.loads(self.config.read_text())['environment']['BIOMIND_PI_MODEL_RECORD'],str(record))
        self.assertEqual(json.loads(record.read_text()),value)

    def test_model_record_mismatch_extra_fields_and_url_credentials_are_refused(self):
        self.runtime.write_text(json.dumps({'sessionDir':str(self.sessions),'provider':'deepseek',
            'model':'deepseek-v4-flash','thinkingLevel':'off'}))
        record=self.root/'model-selection.json'
        value=dict(schema='biomind-active-model-config-v2',provider='deepseek',model='deepseek-v4-flash',
            api='openai-completions',thinking='off',endpoint_hostname='api.deepseek.com',
            origin='active-launch-environment',confirmed_active=True,endpoint_base_url='https://api.deepseek.com')
        for change,code in (({'model':'different'},'RUNTIME_MODEL_RECORD_MISMATCH'),
                            ({'apiKey':'synthetic marker'},'MODEL_RECORD_NOT_NONSECRET_METADATA'),
                            ({'endpoint_base_url':'https://user:synthetic@api.deepseek.com'},'INVALID_MODEL_RECORD_ENDPOINT')):
            record.write_text(json.dumps({**value,**change}))
            with self.assertRaisesRegex(configure.Refused,code):
                configure.configure(self.config,{**self.setup,'modelRecordPath':str(record)})
        self.assertFalse(self.config.exists())

    def test_sensitive_runtime_names_and_symlink_targets_rejected_without_opening(self):
        fake_sensitive=self.root/'.env.api_keys';fake_sensitive.write_text('synthetic test marker only')
        link=self.root/'runtime-link.json';link.symlink_to(fake_sensitive)
        original=Path.open
        def opened(path,*args,**kwargs):
            self.assertNotEqual(path,fake_sensitive)
            self.assertNotEqual(path,link)
            return original(path,*args,**kwargs)
        for path in (fake_sensitive,link):
            with patch.object(Path,'open',opened):
                with self.assertRaisesRegex(configure.Refused,'SENSITIVE_RUNTIME_PATH_REFUSED'):
                    configure.configure(self.config,{**self.setup,'runtimeConfig':str(path)})

    def test_non_metadata_runtime_fields_rejected_without_reading_referenced_files(self):
        self.runtime.write_text(json.dumps({'sessionDir':str(self.sessions),'apiKey':'synthetic marker'}))
        with self.assertRaisesRegex(configure.Refused,'RUNTIME_CONFIG_NOT_NONSECRET_METADATA'):
            configure.configure(self.config,self.setup)

    def test_repeat_is_idempotent_and_preserves_server_identity(self):
        first=configure.configure(self.config,self.setup)
        stamp=self.config.stat().st_mtime_ns
        second=configure.configure(self.config,self.setup)
        self.assertEqual(first['serverId'],second['serverId']);self.assertTrue(second['reused'])
        self.assertEqual(stamp,self.config.stat().st_mtime_ns)
        self.assertEqual(list(self.config.parent.glob('*.backup-*')),[])

    def test_running_service_rejects_changed_password_without_mutation(self):
        configure.configure(self.config,self.setup)
        state=self.config.parent/'state/service.json';state.write_text(json.dumps({'status':'ready'}));state.chmod(0o600)
        original=(self.config.parent/'runtime-environment.json').read_bytes()
        with self.assertRaisesRegex(configure.Refused,'SERVICE_ACTIVE_CONFIGURATION_LOCKED'):
            configure.configure(self.config,{**self.setup,'workbenchPassword':'different-workbench-password'})
        self.assertEqual(original,(self.config.parent/'runtime-environment.json').read_bytes())

    def test_stopped_service_update_keeps_backups_and_retires_old_binding(self):
        first=configure.configure(self.config,self.setup)
        state=self.config.parent/'state/service.json';state.write_text(json.dumps({'status':'stopped'}));state.chmod(0o600)
        second=configure.configure(self.config,{**self.setup,'workbenchPassword':'different-workbench-password'})
        self.assertEqual(first['serverId'],second['serverId']);self.assertFalse(state.exists())
        self.assertEqual(len(list(self.config.parent.glob('service.json.backup-*'))),1)
        self.assertEqual(len(list(self.config.parent.glob('runtime-environment.json.backup-*'))),1)
        self.assertEqual(len(list(state.parent.glob('service.json.backup-*'))),1)

    def test_cross_identity_and_live_lifetime_lock_rejected(self):
        configure.configure(self.config,self.setup)
        with self.assertRaisesRegex(configure.Refused,'CONFIGURATION_IDENTITY_CHANGE_REFUSED'):
            configure.configure(self.config,{**self.setup,'serverId':'biomind-other'})
        with (self.config.parent/'state/backend.lock').open('r+') as lease:
            fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(configure.Refused,'SERVICE_ACTIVE_CONFIGURATION_LOCKED'):
                configure.configure(self.config,{**self.setup,'workbenchPassword':'different-workbench-password'})

    def test_missing_build_or_shared_config_directory_fails_closed(self):
        (self.backend/'.next-pkg/BUILD_ID').unlink()
        with self.assertRaisesRegex(configure.Refused,'WORKBENCH_BACKEND_BUILD_REQUIRED'):
            configure.configure(self.config,self.setup)
        self.assertFalse(self.config.exists())
        self.config.parent.chmod(0o755)
        with self.assertRaisesRegex(configure.Refused,'PRIVATE_DIRECTORY_OR_FILE_REQUIRED'):
            configure.configure(self.config,self.setup)

    def test_cli_never_prints_password_and_enforces_bounded_input(self):
        result=subprocess.run([sys.executable,str(SCRIPT),'--config',str(self.config)],input=json.dumps(self.setup),
            text=True,capture_output=True,check=False)
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertNotIn(self.password,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'],'configured')
        too_large=subprocess.run([sys.executable,str(SCRIPT),'--config',str(self.config)],input='x'*65537,
            text=True,capture_output=True,check=False)
        self.assertEqual(too_large.returncode,2)
        self.assertEqual(json.loads(too_large.stdout)['code'],'SETUP_INPUT_TOO_LARGE')


if __name__=='__main__':
    unittest.main(verbosity=2)
