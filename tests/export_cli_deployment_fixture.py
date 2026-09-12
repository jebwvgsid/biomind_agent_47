"""Safe export of current CLI style, installed-dependency and fatal probes."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import export_planner_boundary_fixture
import export_cli_model_progress_fixture as base
base.TEST_FILES += (
    'tests/export_cli_deployment_fixture.py','tests/replay_cli_deployment.py',
    'tests/test_cli_semantic_styles.py','tests/cli_fatal_plugin.py',
    'tests/run_cli_fatal_diagnostics.py','tests/test_cli_fatal_outlet.py',
    'tests/test_cli_deployment_receipts.py',
)
if __name__=='__main__':base.main()
