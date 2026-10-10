import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('isolated_prepare', ROOT / 'scripts/isolated-candidate-prepare.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class IsolatedCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_copy_has_independent_inode_and_bytes(self):
        (self.source / 'data.js').write_text('original')
        rows = []
        target = self.root / 'target'
        m.copy_tree(self.source, target, inventory=rows)
        self.assertEqual(len(rows), 1)
        self.assertNotEqual((self.source / 'data.js').stat().st_ino, (target / 'data.js').stat().st_ino)
        (target / 'data.js').write_text('edited')
        self.assertEqual((self.source / 'data.js').read_text(), 'original')

    def test_external_symlink_cannot_import_shared_state(self):
        external = self.root / 'external'
        external.write_text('not copied')
        (self.source / 'link').symlink_to(external)
        with self.assertRaisesRegex(RuntimeError, 'COPY_SOURCE_LINK_ESCAPES_SELECTED_TREE'):
            m.copy_tree(self.source, self.root / 'target')

    def test_internal_symlinks_are_physical_copies(self):
        (self.source / 'target.js').write_text('data')
        (self.source / 'link.js').symlink_to('target.js')
        dest = self.root / 'dest'
        m.copy_tree(self.source, dest)
        self.assertFalse((dest / 'link.js').is_symlink())
        self.assertEqual((dest / 'link.js').read_text(), 'data')
        self.assertNotEqual((dest / 'link.js').stat().st_ino, (dest / 'target.js').stat().st_ino)

    def test_credentials_and_cache_are_not_read_or_copied(self):
        for name in ['.env', '.env.api_keys', 'auth.json', '.npmrc']:
            (self.source / name).write_text('should not copy')
        (self.source / 'cache').mkdir()
        (self.source / 'cache/data').write_text('cache')
        dest = self.root / 'dest'
        m.copy_tree(self.source, dest, skipped=('cache',))
        self.assertEqual(list(dest.iterdir()), [])

    def test_existing_candidate_cannot_be_overwritten(self):
        task = self.root / 'task'
        (task / 'deployments/existing').mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, 'CANDIDATE_TARGET_ALREADY_EXISTS'):
            m.prepare(task, task / 'deployments/existing')

    def test_runtime_dependency_closure_excludes_unrelated_packages(self):
        names = ['next', 'react', 'react-dom', 'undici', '@earendil-works/pi-coding-agent',
                 '@earendil-works/pi-agent-core', '@earendil-works/pi-ai', '@earendil-works/pi-tui', '@lydell/node-pty']
        for name in names + ['dep', 'unrelated-ui']:
            pkg = self.source / name
            pkg.mkdir(parents=True)
            metadata = {'name': name, 'version': '1.0.0'}
            if name == 'next': metadata['dependencies'] = {'dep': '^1'}
            (pkg / 'package.json').write_text(json.dumps(metadata))
        dest = self.root / 'dest'
        result = m.runtime_dependencies(self.source, dest, [])
        self.assertEqual(len(result), len(names) + 1)
        self.assertTrue((dest / 'dep/package.json').exists())
        self.assertFalse((dest / 'unrelated-ui').exists())

    def test_native_guard_import_initializes_fresh_scopes(self):
        check = self.root / 'plugin/check'
        source_agent = check / 'agent'
        (source_agent / 'npm/node_modules').mkdir(parents=True)
        (source_agent / 'npm/package.json').write_text('{}')
        (source_agent / 'settings.json').write_text(json.dumps({
            'packages': ['npm:pi-web-access@file:' + str(check.parent / 'pi-web-access-0.38.0.tgz')]}))
        project = self.root / 'project'
        project.mkdir()
        agent = self.root / 'agent'
        receipt = m.guarded_import(ROOT, agent, project, source_agent, self.root, Path('/usr/bin/node'), [])
        self.assertEqual(receipt['origin'], 'born-guarded')
        for path in [agent, project / '.pi']:
            marker = json.loads((path / '.biomind-resource-guard.json').read_text())
            self.assertEqual(marker['root'], str(path))
            self.assertEqual(marker['origin'], 'born-guarded')
        self.assertTrue((agent / 'npm/package.json').is_file())

    def test_existing_resource_root_refused_before_copy(self):
        agent = self.root / 'agent'
        agent.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'FRESH_PLUGIN_SCOPE_REQUIRED'):
            m.guarded_import(ROOT, agent, self.root / 'project', self.source, self.root, Path('/usr/bin/node'), [])
        self.assertEqual(list(agent.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
