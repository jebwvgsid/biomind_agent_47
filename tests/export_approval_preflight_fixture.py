"""Explicit offline export for approval visibility and bounded preflight checks."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_plan_results_fixture as previous

FILES = ('tests/export_approval_preflight_fixture.py',
         'tests/replay_approval_visibility_pty.py',
         'tests/test_approval_visibility.py',
         'tests/test_approval_owner_lifecycle.py',
         )
previous.previous.previous.TEST_FILES += FILES

if __name__ == '__main__':
    previous.previous.main()
