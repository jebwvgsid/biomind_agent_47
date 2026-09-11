"""Export only CLI acceptance sources; never copy configuration or sessions."""
from pathlib import Path
import shutil
import sys
root=Path(__file__).resolve().parents[1]
target=Path(sys.argv[1]).resolve()
if target.exists() or not str(target).startswith('/tmp/'):
    raise SystemExit('Use a new /tmp directory')
files=['bio_agents.py','agent_core/__init__.py','agent_core/cli_logging.py','agent_core/cli_display.py','agent_core/cli_terminal.py','agent_core/human_io.py','agent_core/step_effects.py','execution/executor.py','execution/profiling.py','execution/runtime_contract.py','execution/shell_safety.py','tests/test_cli_logging.py','tests/test_cli_display.py','tests/test_cli_terminal.py','tests/cli_fixture_entry.py','tests/cli_pty_acceptance.py','tests/cli_pty_edges.py','tests/cli_pty_latency.py','tests/cli_output_benchmark.py']
for name in files:
    path=target/name;path.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(root/name,path)
shutil.copyfile(root/'tests/cli_offline_conftest.py',target/'conftest.py')
print(target)
