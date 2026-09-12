"""Allowlisted export for Consultant handoff and synthetic continue regression."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_plan_results_fixture as previous
previous.previous.previous.TEST_FILES += (
    'tests/export_consultant_handoff_fixture.py',
    'tests/test_consultant_skill_handoff.py',
    'tests/test_consultant_stop_resume.py',
)
if __name__ == '__main__':
    previous.previous.main()
