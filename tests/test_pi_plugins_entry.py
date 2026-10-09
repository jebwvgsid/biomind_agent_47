import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_core import pi_entry, pi_plugins


class PluginEntryTests(unittest.TestCase):
    def test_package_aliases_dispatch_before_model_project_or_session_entry(self):
        for command in ('install', 'remove', 'uninstall', 'list', 'config', 'update', 'plugins'):
            with patch.object(pi_plugins, 'main', return_value=17) as package, patch.object(pi_entry, 'launch_pi', side_effect=AssertionError('model gate entered')):
                self.assertEqual(pi_entry.maybe_dispatch([command]), 17)
                self.assertEqual(package.call_args.args[0], [command])

    def test_self_update_forms_rejected_before_config_or_runtime_reads(self):
        for argv in (['update'], ['update', '--self'], ['update', '--all'], ['update', 'pi'], ['update', 'self'], ['plugins', '--json', '--self']):
            with self.subTest(argv=argv), patch.object(pi_plugins, 'read_context', side_effect=AssertionError('config read')), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(pi_plugins.main(argv), 2)
                self.assertEqual(json.loads(output.getvalue())['code'], 'PINNED_PI_SELF_UPDATE_REFUSED')

    def test_update_local_is_not_misrepresented_as_native_scope_option(self):
        args = pi_plugins.parser().parse_args(['update', '--extensions', '-l'])
        with self.assertRaisesRegex(ValueError, 'NATIVE_UPDATE_HAS_NO_SCOPE_OPTION'):
            pi_plugins.make_request(args)

    def test_auth_and_models_paths_are_rejected_without_reading_them(self):
        for name in ('auth.json', 'models.json', '.env.api_keys', 'private-secret.json'):
            args = pi_plugins.parser().parse_args(['list', '--pi-config', '/synthetic/' + name])
            with self.assertRaisesRegex(ValueError, 'NONSECRET_RUNTIME_CONFIG_REQUIRED'):
                pi_plugins.read_context(args)

    def test_mutation_timeout_is_unknown_repair_required_not_safe_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / 'dist').mkdir()
            (runtime / 'dist/plugin-cli.js').write_text('')
            package = runtime / 'node_modules/@earendil-works/pi-coding-agent'
            package.mkdir(parents=True)
            (package / 'package.json').write_text(json.dumps({'version': '0.87.1'}))
            node = runtime / 'fake-node'; node.write_text(''); node.chmod(0o700)
            responses = [subprocess.CompletedProcess([], 0, stdout='v22.22.0\n'), subprocess.TimeoutExpired(['node'], 900)]
            with patch.object(pi_plugins.subprocess, 'run', side_effect=responses), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(pi_plugins.main(['install', './synthetic', '--pi-node', str(node)], runtime=runtime), 2)
            value = json.loads(output.getvalue())
            self.assertEqual(value['status'], 'repair_required')
            self.assertEqual(value['code'], 'PACKAGE_CLI_TIMEOUT_OUTCOME_UNKNOWN')
            self.assertTrue(value['outcomeUnknown'])

    def test_missing_child_receipt_after_mutation_is_unknown_not_auto_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / 'dist').mkdir()
            (runtime / 'dist/plugin-cli.js').write_text('')
            package = runtime / 'node_modules/@earendil-works/pi-coding-agent'
            package.mkdir(parents=True)
            (package / 'package.json').write_text(json.dumps({'version': '0.87.1'}))
            node = runtime / 'fake-node'; node.write_text(''); node.chmod(0o700)
            responses = [subprocess.CompletedProcess([], 0, stdout='v22.22.0\n'), subprocess.CompletedProcess([], 0)]
            with patch.object(pi_plugins.subprocess, 'run', side_effect=responses), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(pi_plugins.main(['install', './synthetic', '--pi-node', str(node)], runtime=runtime), 2)
            value = json.loads(output.getvalue())
            self.assertEqual(value['status'], 'repair_required')
            self.assertEqual(value['code'], 'PACKAGE_CLI_RESULT_OUTCOME_UNKNOWN')
            self.assertTrue(value['outcomeUnknown'])


if __name__ == '__main__':
    unittest.main()
