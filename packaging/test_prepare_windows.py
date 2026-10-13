"""Exercise release input isolation and failure preservation with synthetic files."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).with_name('prepare-windows.py')


class PrepareWindowsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='biomind-package-inputs-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        files = {
            'desktop/package.json': json.dumps(dict(name='biomind-studio', version='1.2.0',
                private=True, license='MIT', description='fixture', main='electron/remote-main.cjs')),
            'desktop/package-lock.json': '{"packages":{}}',
            'desktop/LICENSE': 'synthetic license',
            'desktop/electron/remote-main.cjs': 'module.exports = {};',
            'desktop/electron/remote-main.test.cjs': 'must not be packaged',
            'desktop/out/renderer/index.html': 'HISTORICAL_BUILD_MUST_NOT_BE_USED',
            'backend/pi-studio/lib/biomind-session-adapter.ts': 'export {};',
            'server/server-service.py': '# synthetic service',
            'server/next-service.mjs': '// synthetic service',
            'server/configure-service.py': '# synthetic configure',
            'server/setup-preset.json': 'HISTORICAL_PRESET_MUST_NOT_BE_USED',
            'fresh-build/renderer/index.html': 'EXPLICIT_COMPILED_INPUT',
            'release/RELEASE.json': json.dumps(dict(releaseId='release-fixture',
                sourceCommit='b' * 40, sourceFingerprint='c' * 64)),
        }
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (self.root / 'packaging').mkdir()
        shutil.copy2(SOURCE, self.root / 'packaging/prepare-windows.py')
        self.preset = dict(schema='BioMindStudioSetupPresetV1',
            profile=dict(alias='fixture', servicePath='/fixture/service.py', serviceConfig='/fixture/service.json'),
            setup=dict(mainRoot=str(self.root / 'release')))
        self.write_preset()

    def write_preset(self):
        (self.root / 'new-preset.json').write_text(json.dumps(self.preset))

    def run_prepare(self):
        return subprocess.run([sys.executable, '-B', str(self.root / 'packaging/prepare-windows.py'),
            '--stage', str(self.root / 'stage'), '--evidence', str(self.root / 'receipt.json'),
            '--desktop-build', str(self.root / 'fresh-build'), '--core-release', str(self.root / 'release'),
            '--setup-preset', str(self.root / 'new-preset.json'), '--studio-commit', 'a' * 40],
            capture_output=True, text=True, timeout=15)

    def test_explicit_inputs_replace_no_historical_input(self):
        result = self.run_prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'stage/out/renderer/index.html').read_text(), 'EXPLICIT_COMPILED_INPUT')
        self.assertEqual(json.loads((self.root / 'stage/server/setup-preset.json').read_text()), self.preset)
        self.assertFalse((self.root / 'stage/electron/remote-main.test.cjs').exists())
        identity = json.loads((self.root / 'receipt.json').read_text())
        self.assertEqual(identity['version'], '1.2.0')
        self.assertEqual(identity['targetCore']['root'], str(self.root / 'release'))
        self.assertEqual(identity['studioSource']['commit'], 'a' * 40)
        for row in identity['files']:
            raw = (self.root / 'stage' / row['path']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row['sha256'])
        self.assertEqual((self.root / 'desktop/out/renderer/index.html').read_text(), 'HISTORICAL_BUILD_MUST_NOT_BE_USED')
        self.assertEqual((self.root / 'server/setup-preset.json').read_text(), 'HISTORICAL_PRESET_MUST_NOT_BE_USED')

    def test_existing_stage_is_retained(self):
        (self.root / 'stage').mkdir()
        marker = self.root / 'stage/marker'
        marker.write_text('keep')
        self.assertNotEqual(self.run_prepare().returncode, 0)
        self.assertEqual(marker.read_text(), 'keep')
        self.assertFalse((self.root / 'receipt.json').exists())

    def test_existing_receipt_is_retained_without_creating_stage(self):
        receipt = self.root / 'receipt.json'
        receipt.write_text('keep')
        self.assertNotEqual(self.run_prepare().returncode, 0)
        self.assertEqual(receipt.read_text(), 'keep')
        self.assertFalse((self.root / 'stage').exists())

    def test_nested_secret_is_rejected_before_output(self):
        self.preset['setup']['providers'] = [{'api_key': 'synthetic-test-key'}]
        self.write_preset()
        self.assertNotEqual(self.run_prepare().returncode, 0)
        self.assertFalse((self.root / 'stage').exists())

    def test_mismatched_core_preset_is_rejected_before_output(self):
        self.preset['setup']['mainRoot'] = str(self.root / 'old-release')
        self.write_preset()
        self.assertNotEqual(self.run_prepare().returncode, 0)
        self.assertFalse((self.root / 'stage').exists())


if __name__ == '__main__':
    unittest.main()
