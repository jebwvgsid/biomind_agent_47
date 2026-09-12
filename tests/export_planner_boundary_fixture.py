"""Allowlisted offline export for Planner protocol, lifecycle and input boundaries."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_consultant_handoff_fixture as previous
import export_cli_model_progress_fixture as base
base.TEST_FILES += (
    'tests/export_planner_boundary_fixture.py',
    'tests/test_planner_boundary.py',
    'tests/test_planner_lifecycle_boundary.py',
    'tests/test_input_boundary.py',
    'tests/replay_input_boundary_pty.py',
    'tests/replay_cli_v2_pty.py',
    'tests/replay_approval_visibility_pty.py',
    'tests/test_approval_visibility.py',
    'tests/test_approval_owner_lifecycle.py',
)
if __name__ == '__main__':
    previous.previous.previous.main()
