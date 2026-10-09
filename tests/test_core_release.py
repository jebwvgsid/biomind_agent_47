"""Filesystem/selector tests, not model or Windows acceptance."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('core_release', ROOT / 'scripts/core-release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, name, content):
        root = self.root / name
        runtime = root / release.RUNTIME_REL
        (runtime / 'dist').mkdir(parents=True)
        (runtime / 'node_modules/sample').mkdir(parents=True)
        (root / 'bio_agents.py').write_text(content)
        (runtime / 'dist/host.js').write_text('fixture host')
        (runtime / 'node_modules/sample/index.js').write_text('dependency version 1')
        manifest = {'schemaVersion': 1, 'buildId': name, 'sourceFingerprint': name,
                    'sources': [{'path': 'bio_agents.py', 'sha256': release.digest(root / 'bio_agents.py')}],
                    'artifacts': [{'path': 'dist/host.js', 'sha256': release.digest(runtime / 'dist/host.js')}]}
        (runtime / 'dist/build-manifest.json').write_text(json.dumps(manifest))
        (root / 'DEPENDENCIES.json').write_text(json.dumps(release.inventory(runtime / 'node_modules')))
        (root / 'RELEASE.json').write_text(json.dumps({'releaseId': name,
            'manifestSha256': release.digest(runtime / 'dist/build-manifest.json'),
            'dependenciesInventorySha256': release.digest(root / 'DEPENDENCIES.json')}))
        return root

    def test_select_and_rollback_preserve_old_resolved_root_and_user_settings(self):
        old = self.fixture('old', 'old code')
        new = self.fixture('new', 'new code')
        link = self.root / 'current'
        release.select(link, old)
        worker_root = link.resolve()
        user = self.root / 'settings.json'
        user.write_text('{"user-added":"retained"}')
        release.select(link, new)
        self.assertEqual((worker_root / 'bio_agents.py').read_text(), 'old code')
        self.assertEqual(link.resolve(), new)
        outcome = release.select(link, old)
        self.assertEqual(outcome['previousRelease'], str(new))
        self.assertFalse(outcome['settingsOrPluginsRestored'])
        self.assertEqual(user.read_text(), '{"user-added":"retained"}')

    def test_changed_source_cannot_be_selected(self):
        root = self.fixture('candidate', 'original')
        (root / 'bio_agents.py').write_text('changed after build')
        with self.assertRaisesRegex(ValueError, 'CONTENT_MISMATCH'):
            release.select(self.root / 'current', root)
        self.assertFalse((self.root / 'current').exists())

    def test_dependency_file_tampering_is_not_hidden_by_version_label(self):
        root = self.fixture('candidate', 'original')
        (root / release.RUNTIME_REL / 'node_modules/sample/index.js').write_text('wrong dependency')
        with self.assertRaisesRegex(ValueError, 'DEPENDENCIES_MISMATCH'):
            release.verify(root)

    def test_selection_will_not_replace_a_real_directory(self):
        root = self.fixture('candidate', 'original')
        link = self.root / 'current'
        link.mkdir()
        (link / 'user-file').write_text('preserve')
        with self.assertRaisesRegex(ValueError, 'NOT_A_SYMLINK'):
            release.select(link, root)
        self.assertEqual((link / 'user-file').read_text(), 'preserve')

    def test_release_paths_and_dependency_links_cannot_escape(self):
        root = self.fixture('candidate', 'original')
        with self.assertRaisesRegex(ValueError, 'INVALID_PATH'):
            release.local(root, '../other')
        with self.assertRaisesRegex(ValueError, 'INVALID_PATH'):
            release.local(root, '/other')
        (root / release.RUNTIME_REL / 'node_modules/external').symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, 'SYMLINK_ESCAPES'):
            release.inventory(root / release.RUNTIME_REL / 'node_modules')


if __name__ == '__main__':
    unittest.main()
