import contextlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('candidate_runner', Path(__file__).resolve().parents[1] / 'scripts/run-isolated-candidate.py')
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class CandidateRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'PREPARED.json').write_text('{}')
        (self.root / 'runtime.json').write_text(json.dumps({'agentDir': str(self.root / 'agent')}))
        self.config = {'mode': 'local', 'stateRoot': str(self.root / 'state'),
                       'environmentFile': str(self.root / 'runtime-environment.json'), 'cwd': str(self.root / 'backend'),
                       'environment': {'BIOMIND_RUNTIME_CONFIG': str(self.root / 'runtime.json'),
                                       'BIOMIND_SESSION_ROOT': str(self.root / 'agent/sessions'),
                                       'PI_WEB_UPLOADS_DIR': str(self.root / 'uploads'), 'BIOMIND_DISABLE_DOTENV': '1'}}

    def test_reject_shared_resource_symlink(self):
        (self.root / 'agent').symlink_to('/tmp')
        with self.assertRaisesRegex(runner.Refused, 'ISOLATED_RUNTIME_PATH_REQUIRED'):
            runner.validate_layout(self.root, self.config)

    def test_reject_shared_writable_state(self):
        self.config['stateRoot'] = '/tmp/production-state'
        with self.assertRaisesRegex(runner.Refused, 'ISOLATED_SERVICE_PATH_REQUIRED'):
            runner.validate_layout(self.root, self.config)

    def test_require_explicit_dotenv_disable(self):
        self.config['environment'].pop('BIOMIND_DISABLE_DOTENV')
        with self.assertRaisesRegex(runner.Refused, 'ISOLATED_DOTENV_DISABLE_REQUIRED'):
            runner.validate_layout(self.root, self.config)

    def service(self, status='stopped', lease=False):
        s = Mock()
        s.locked.side_effect = lambda config: contextlib.nullcontext()
        s.read_record.return_value = None
        s.inspect.return_value = {'status': status}
        s.backend_lease_held.return_value = lease
        return s

    def test_live_service_credentials_not_read_or_replaced(self):
        s = self.service('ready')
        with self.assertRaisesRegex(runner.Refused, 'CREDENTIAL_CHANGE_REQUIRES_STOPPED_CANDIDATE'):
            runner.configure_credentials(self.root, s, self.config, 'new-local-test-password', '')
        s.private.assert_not_called()
        s.atomic.assert_not_called()

    def test_orphan_backend_lease_blocks_credentials_before_write(self):
        s = self.service(lease=True)
        with self.assertRaisesRegex(runner.Refused, 'CREDENTIAL_CHANGE_REQUIRES_STOPPED_CANDIDATE'):
            runner.configure_credentials(self.root, s, self.config, 'new-local-test-password', '')
        s.atomic.assert_not_called()

    def test_only_explicit_candidate_credentials_changed(self):
        s = self.service()
        path = Path(self.config['environmentFile'])
        path.write_text(json.dumps({'PI_WEB_PASSWORD': 'old-password-placeholder', 'OTHER_SETTING': 'preserved'}))
        runner.configure_credentials(self.root, s, self.config, 'new-local-test-password', 'test-key-placeholder')
        self.assertEqual(s.atomic.call_args.args, (path, {'PI_WEB_PASSWORD': 'new-local-test-password',
                         'OTHER_SETTING': 'preserved', 'DEEPSEEK_API_KEY': 'test-key-placeholder'}))

    def test_error_receipt_does_not_include_arbitrary_text(self):
        self.assertEqual(runner.safe_error(ValueError('credential=do-not-emit')), 'ValueError')

    def prepare_verification(self):
        (self.root / 'private').mkdir()
        (self.root / 'evidence').mkdir()
        self.config['environment'].update(BIOMIND_ENTRYPOINT='/fixed/core/bio_agents.py', BIOMIND_LAUNCHER_PYTHON='/usr/bin/python3')
        (self.root / 'private/service.json').write_text(json.dumps(self.config))
        (self.root / 'PREPARED.json').write_text(json.dumps({'project': str(self.root / 'projects/acceptance')}))

    def test_acceptance_uses_test_project_and_repeated_stage_only_resumes(self):
        self.prepare_verification()
        (self.root / 'evidence/http-acceptance-search.json').write_text('{}')
        with patch.object(runner.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            runner.verify(self.root, True, True, True)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--project') + 1], str(self.root / 'projects/acceptance'))
        self.assertIn('--resume', command)
        self.assertNotIn('--real-search', command)

    def test_identity_receipt_does_not_suppress_requested_model_test(self):
        self.prepare_verification()
        (self.root / 'evidence/http-acceptance-identity.json').write_text('{}')
        with patch.object(runner.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            runner.verify(self.root, real_model=True)
        command = run.call_args.args[0]
        self.assertNotIn('--resume', command)
        self.assertIn('--real-model', command)
        self.assertEqual(command[command.index('--evidence') + 1], str(self.root / 'evidence/http-acceptance-model.json'))


if __name__ == '__main__':
    unittest.main()
