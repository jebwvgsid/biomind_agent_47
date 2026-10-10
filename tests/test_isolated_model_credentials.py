import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('configure_model', ROOT / 'scripts/configure-isolated-model.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
CORE = Path('/public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/releases/biomind-30aeedb2e667e60c')
MODULE = CORE / 'integrations/pi_runtime/node_modules/@earendil-works/pi-coding-agent/dist/core/auth-storage.js'
NODE = Path('/public/home/liusiqi/03.毕设/Biomind_v1/integrations/pi_runtime/.runtime/node/bin/node')


class ModelCredentialTests(unittest.TestCase):
    def test_input_error_categories_are_safe_and_do_not_echo_input(self):
        cases = [('', 'CREDENTIAL_EMPTY'), ('short', 'CREDENTIAL_LENGTH_INVALID'),
            ('x' * 8193, 'CREDENTIAL_LENGTH_INVALID'), ('test-secret\nvalue', 'CREDENTIAL_WHITESPACE'),
            ('test-secret value', 'CREDENTIAL_WHITESPACE'), ('test-secret\0value', 'CREDENTIAL_CONTROL_CHARACTER'),
            ('test-\x1b[200~secret', 'CREDENTIAL_CONTROL_CHARACTER'), ('!test-secret', 'CREDENTIAL_COMMAND_LITERAL_REFUSED')]
        for value, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(helper.Refused, '^' + code + '$'):
                helper.literal_key(value)

    def test_hidden_input_retries_only_local_validation_without_changing_valid_key(self):
        valid = 'test-literal-unmodified-value'
        output = io.StringIO()
        with patch.object(helper.sys.stdin, 'isatty', return_value=True), \
                patch.object(helper.getpass, 'getpass', side_effect=['test-invalid value', valid]) as prompt, \
                patch.object(helper.sys, 'stdout', output):
            self.assertEqual(helper.read_hidden_key(), valid)
        self.assertEqual(prompt.call_count, 2)
        self.assertIn('空格或换行', output.getvalue())
        self.assertNotIn('test-invalid value', output.getvalue())
        self.assertNotIn(valid, output.getvalue())

    def test_hidden_input_bounded_and_failed_inputs_not_printed(self):
        output = io.StringIO()
        with patch.object(helper.sys.stdin, 'isatty', return_value=True), \
                patch.object(helper.getpass, 'getpass', return_value='test-invalid value') as prompt, \
                patch.object(helper.sys, 'stdout', output), \
                self.assertRaisesRegex(helper.Refused, '^CREDENTIAL_WHITESPACE$'):
            helper.read_hidden_key()
        self.assertEqual(prompt.call_count, 3)
        self.assertNotIn('test-invalid value', output.getvalue())

    def test_hidden_input_cancel_and_echo_fallback_refused(self):
        def warn_echo(*_args):
            warnings.warn('test echo fallback', helper.getpass.GetPassWarning)
            self.fail('must not continue into echo fallback')
        for effect, code in [(EOFError, 'CREDENTIAL_INPUT_CANCELLED'),
                (KeyboardInterrupt, 'CREDENTIAL_INPUT_CANCELLED'), (warn_echo, 'HIDDEN_INPUT_UNAVAILABLE')]:
            with self.subTest(code=code), patch.object(helper.sys.stdin, 'isatty', return_value=True), \
                    patch.object(helper.getpass, 'getpass', side_effect=effect) as prompt, \
                    patch.object(helper.sys, 'stdout', io.StringIO()), \
                    self.assertRaisesRegex(helper.Refused, '^' + code + '$'):
                helper.read_hidden_key()
            self.assertEqual(prompt.call_count, 1)

    def test_non_tty_never_requests_key(self):
        with patch.object(helper.sys.stdin, 'isatty', return_value=False), \
                patch.object(helper.getpass, 'getpass') as prompt, \
                self.assertRaisesRegex(helper.Refused, '^INTERACTIVE_TERMINAL_REQUIRED$'):
            helper.read_hidden_key()
        prompt.assert_not_called()

    def run_main_fixture(self, folder, inputs, *, existing_receipt=False, save_failure=False):
        root = Path(folder)
        (root / 'evidence').mkdir()
        if existing_receipt:
            (root / 'evidence/http-acceptance-credential-first-key.json').write_text('{}')
        verify = SimpleNamespace(under=lambda p, _root: str(p), private_json=lambda _p: {})
        config = {'environment': {'BIOMIND_LAUNCHER_PYTHON': '/python'}}
        prepared = {'coreRoot': '/core', 'project': str(root / 'project')}
        output = io.StringIO()
        with patch.object(helper.sys, 'argv', ['helper', '--deployment', str(root), '--verify']), \
                patch.object(helper.sys.stdin, 'isatty', return_value=True), \
                patch.object(helper.sys, 'stdout', output), \
                patch.object(helper, 'load_module', return_value=verify), \
                patch.object(helper, 'layout', return_value=(prepared, config, {}, root / 'auth.json', MODULE)), \
                patch.object(helper, 'connected', return_value={'serverId': 'test-server', 'instanceId': 'test-instance', 'host': 'test-host'}), \
                patch.object(helper.getpass, 'getpass', side_effect=inputs) as prompt, \
                patch.object(helper, 'save_native', side_effect=helper.Refused('TEST_NATIVE_SAVE_FAILED') if save_failure else None) as save, \
                patch.object(helper.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            status = helper.main()
        return status, prompt, save, run, output.getvalue()

    def test_main_invalid_input_cannot_save_or_launch_verifier(self):
        with tempfile.TemporaryDirectory() as folder:
            status, prompt, save, run, output = self.run_main_fixture(folder, ['', '', ''])
        self.assertEqual(status, 2)
        self.assertEqual(prompt.call_count, 3)
        save.assert_not_called()
        run.assert_not_called()
        self.assertEqual(json.loads(output.splitlines()[-1])['code'], 'CREDENTIAL_EMPTY')

    def test_main_valid_input_saved_once_and_save_failure_not_retried(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                status, prompt, save, run, output = self.run_main_fixture(folder,
                    ['test-invalid value', 'test-valid-value'], save_failure=failure)
            self.assertEqual(status, 2 if failure else 0)
            self.assertEqual(prompt.call_count, 2)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(run.call_count, 0 if failure else 1)
            self.assertNotIn('test-valid-value', output)
            self.assertNotIn('test-invalid value', output)

    def test_main_existing_receipt_never_prompts_or_saves_or_replays(self):
        with tempfile.TemporaryDirectory() as folder:
            status, prompt, save, run, _output = self.run_main_fixture(folder, [], existing_receipt=True)
        self.assertEqual(status, 0)
        prompt.assert_not_called()
        save.assert_not_called()
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertIn('--resume', command)
        self.assertNotIn('--real-model', command)

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

    @unittest.skipUnless(NODE.exists() and MODULE.exists(), 'actual pinned Pi runtime unavailable')
    def test_actual_native_rejects_terminal_controls_without_write(self):
        with tempfile.TemporaryDirectory() as folder:
            result, auth = self.native(Path(folder), {}, 'test-\x1b[200~clipboard-value')
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)['code'], 'INVALID_LITERAL_CREDENTIAL')
            self.assertEqual(json.loads(auth.read_text()), {})


if __name__ == '__main__':
    unittest.main()
