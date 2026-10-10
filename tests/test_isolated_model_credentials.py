import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('configure_model', ROOT / 'scripts/configure-isolated-model.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
CORE = Path('/public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/releases/biomind-30aeedb2e667e60c')
MODULE = CORE / 'integrations/pi_runtime/node_modules/@earendil-works/pi-coding-agent/dist/core/auth-storage.js'
NODE = Path('/public/home/liusiqi/03.毕设/Biomind_v1/integrations/pi_runtime/.runtime/node/bin/node')


class ModelCredentialTests(unittest.TestCase):
    def test_reject_command_empty_newline_or_whitespace(self):
        for key in ('', '!echo secret', 'test\nsecret', 'test secret', 'test\0secret', 'x' * 8193):
            with self.subTest(key_length=len(key)), self.assertRaises(helper.Refused):
                helper.literal_key(key)

    def test_key_only_in_private_stdin_never_args_or_environment(self):
        key = 'test-fake-never-network-secret'
        config = {'environment': {'BIOMIND_PI_NODE': str(NODE)}}
        with patch.object(helper.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"result":"PASS"}')) as run:
            helper.save_native(config, MODULE, Path('/candidate/auth.json'), key)
        args, kwargs = run.call_args
        self.assertNotIn(key, json.dumps(args))
        self.assertNotIn(key, json.dumps(kwargs['env']))
        self.assertEqual(json.loads(kwargs['input'])['key'], key)

    def test_native_failure_does_not_print_arbitrary_error_or_key(self):
        config = {'environment': {'BIOMIND_PI_NODE': str(NODE)}}
        with patch.object(helper.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2, 'secret raw output', 'secret')):
            with self.assertRaisesRegex(helper.Refused, '^NATIVE_CREDENTIAL_SAVE_UNCONFIRMED$'):
                helper.save_native(config, MODULE, Path('/candidate/auth.json'), 'test-secret-value')

    def test_repeat_acceptance_is_read_only_and_first_uses_existing_owners(self):
        config = {'environment': {'BIOMIND_LAUNCHER_PYTHON': '/python'}}
        prepared = {'coreRoot': '/core', 'project': '/candidate/project'}
        args = (Path('/candidate'), prepared, config, Path('/candidate/receipt.json'))
        command = helper.verify_command(*args, existing=True)
        self.assertIn('--resume', command)
        self.assertNotIn('--real-model', command)
        command = helper.verify_command(*args, existing=False)
        self.assertIn('--reuse-sessions-from', command)
        self.assertIn('--probe-native-credential', command)
        self.assertNotIn('--refresh-credentials', command)

    def native(self, folder, initial, key, mode=0o600, symlink=False):
        auth = folder / 'auth.json'
        auth.write_text(json.dumps(initial))
        auth.chmod(mode)
        target = auth
        if symlink:
            target = folder / 'link.json'
            target.symlink_to(auth)
        result = subprocess.run([str(NODE), str(ROOT / 'scripts/set-isolated-native-credential.mjs'), str(MODULE), str(target)],
            input=json.dumps({'key': key}), text=True, capture_output=True, timeout=20)
        self.assertNotIn(key, result.stdout + result.stderr)
        return result, auth

    @unittest.skipUnless(NODE.exists() and MODULE.exists(), 'actual pinned Pi runtime unavailable')
    def test_actual_native_merge_preserves_other_provider_and_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            other = {'other-provider': {'type': 'api_key', 'key': 'test-other-value'}}
            result, auth = self.native(Path(folder), other, 'test-deepseek-only')
            self.assertEqual(result.returncode, 0, result.stdout)
            value = json.loads(auth.read_text())
            self.assertEqual(value['other-provider'], other['other-provider'])
            self.assertEqual(value['deepseek'], {'type': 'api_key', 'key': 'test-deepseek-only'})
            self.assertEqual(auth.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(NODE.exists() and MODULE.exists(), 'actual pinned Pi runtime unavailable')
    def test_actual_native_refuses_existing_provider_without_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            original = {'deepseek': {'type': 'api_key', 'key': 'test-original'}}
            result, auth = self.native(Path(folder), original, 'test-new-value')
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)['code'], 'CANDIDATE_PROVIDER_CREDENTIAL_ALREADY_EXISTS')
            self.assertEqual(json.loads(auth.read_text()), original)

    @unittest.skipUnless(NODE.exists() and MODULE.exists(), 'actual pinned Pi runtime unavailable')
    def test_actual_native_refuses_symlink_and_public_file(self):
        for symlink, mode in ((True, 0o600), (False, 0o644)):
            with self.subTest(symlink=symlink, mode=mode), tempfile.TemporaryDirectory() as folder:
                result, auth = self.native(Path(folder), {}, 'test-private-value', mode, symlink)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(auth.read_text()), {})

    @unittest.skipUnless(NODE.exists() and MODULE.exists(), 'actual pinned Pi runtime unavailable')
    def test_actual_native_rejects_command_credential_without_write(self):
        with tempfile.TemporaryDirectory() as folder:
            result, auth = self.native(Path(folder), {}, '!printf must-not-execute')
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)['code'], 'INVALID_LITERAL_CREDENTIAL')
            self.assertEqual(json.loads(auth.read_text()), {})


if __name__ == '__main__':
    unittest.main()
