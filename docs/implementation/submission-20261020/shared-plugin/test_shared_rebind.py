"""Exercise migration refusal paths with real native guard initialization."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

def load(name, path):
    s = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m

B = Path('/public/home/liusiqi/03.毕设')
CORE = Path(json.loads((B / 'BioMind_Production_20261020/build-result.json').read_text())['root'])
M = load('rebinder', Path(__file__).with_name('rebind-shared-agent.py'))
G = load('guard', CORE / 'agent_core/pi_plugin_guard.py')

class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = self.root / 'old'
        self.old.mkdir(mode=0o700)
        self.sessions = self.old / 'sessions'
        self.sessions.mkdir()
        self.new = self.root / 'new'
        request = {'agentDir': str(self.new), 'cwd': str(self.root), 'scopes': ['user']}
        row = G.prepare(request)['scopes'][0]
        fd = os.open(row['gate'], os.O_CREAT | os.O_RDWR, 0o600)
        gate = {'fd': fd, 'path': row['gate'], 'mode': 'exclusive'}
        try:
            G.lock_fds({'files': [gate]})
            G.initialize({**request, 'gates': [gate]})
        finally:
            os.close(fd)
        (self.new / 'sessions').symlink_to(self.sessions, target_is_directory=True)
        for name in ('settings.json', 'web-search.json'):
            (self.new / name).write_text('{}')

    def tearDown(self):
        self.tmp.cleanup()

    def check(self):
        return M.shared_agent(CORE, self.old, self.sessions, self.new)

    def test_fresh_guard_and_original_sessions_accepted(self):
        self.assertEqual(self.check()[0], self.new)

    def test_legacy_unverified_scope_refused(self):
        (self.new / '.biomind-resource-guard.json').unlink()
        with self.assertRaisesRegex(M.Refused, 'GUARDED_AGENT_REQUIRED'):
            self.check()

    def test_different_session_root_refused(self):
        other = self.root / 'other-sessions'
        other.mkdir()
        (self.new / 'sessions').unlink()
        (self.new / 'sessions').symlink_to(other)
        with self.assertRaisesRegex(M.Refused, 'SESSION_LAYOUT_CHANGE_REFUSED'):
            self.check()

    def test_same_legacy_scope_refused(self):
        with self.assertRaisesRegex(M.Refused, 'NEW_AGENT_SCOPE_REQUIRED'):
            M.shared_agent(CORE, self.old, self.sessions, self.old)

if __name__ == '__main__':
    unittest.main()
