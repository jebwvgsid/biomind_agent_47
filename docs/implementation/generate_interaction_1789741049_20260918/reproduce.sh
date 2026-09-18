#!/usr/bin/env bash
set -euo pipefail

# Code/fixture export only. The bootstrap denies network, real configuration,
# original task data, and scientific subprocesses. No software installation.
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repository=$(git -C "$script_dir" rev-parse --show-toplevel)
revision=${1:-HEAD}
python_bin=${BIOMIND_TEST_PYTHON:-/public/home/liusiqi/anaconda3/envs/biomind/bin/python}
run_dir=$(mktemp -d /tmp/biomind-interaction-1789741049-XXXXXX)
"$python_bin" "$repository/tests/export_preflight_incremental.py" "$run_dir/code" --tree "$revision"
mkdir -p "$run_dir/tmp" "$run_dir/artifacts"
export TMPDIR="$run_dir/tmp" PYTHONDONTWRITEBYTECODE=1 BIOMIND_DISABLE_DOTENV=1
export BIOMIND_OFFLINE_RESULTS="$run_dir/test-results.json"
export BIOMIND_CLI_PROGRESS_ARTIFACTS="$run_dir/artifacts"
tests=(tests/test_generate_interaction_entrypoints.py
    tests/test_cli_v2_display.py tests/test_cli_generate_integrity.py)
if [[ ${BIOMIND_EXTENDED_REGRESSION:-0} == 1 ]]; then
    tests+=(tests/test_action_scoped_runtime_bindings.py
        tests/test_generate_production_context_wiring.py
        tests/test_schema2_adapter_semantic_coverage.py
        tests/test_schema2_production_graph.py
        tests/test_declared_effect_emission.py
        tests/test_preflight_incremental_graph.py
        tests/test_progressive_skill_acceptance_scenarios.py
        tests/test_progressive_skill_access.py)
fi
"$python_bin" "$run_dir/code/tests/offline_managed_bootstrap.py" "$run_dir/code" -q "${tests[@]}"
