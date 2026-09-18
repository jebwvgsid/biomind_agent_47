#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "$0")/../../.." && pwd)
biomind_test_python=${BIOMIND_TEST_PYTHON:-/public/home/liusiqi/anaconda3/envs/biomind/bin/python}
revision=${1:-HEAD}
fixture_root=$(mktemp -d /tmp/biomind-preflight-replay.XXXXXX)
mkdir -p "$fixture_root/tmp" "$fixture_root/artifacts"
cd "$repo"
"$biomind_test_python" tests/export_preflight_incremental.py "$fixture_root/source" --tree "$revision"
TMPDIR="$fixture_root/tmp" PYTHONDONTWRITEBYTECODE=1 BIOMIND_DISABLE_DOTENV=1 \
BIOMIND_OFFLINE_RESULTS="$fixture_root/test_results.json" BIOMIND_CLI_PROGRESS_ARTIFACTS="$fixture_root/artifacts" \
"$biomind_test_python" "$fixture_root/source/tests/offline_managed_bootstrap.py" "$fixture_root/source" -q \
  tests/test_preflight_incremental_protocol.py tests/test_preflight_incremental_results.py \
  tests/test_preflight_incremental_graph.py tests/test_preflight_incremental_auxiliary.py \
  tests/test_plan_results_name.py tests/test_context_256k_entrypoints.py \
  tests/test_cli_public_response.py tests/test_skill_resource_binding.py \
  tests/test_skill_script_invocation.py tests/test_recovery_projection.py tests/test_cli_v2_display.py \
  tests/test_provider_attempt_reliability.py::ProviderAttemptReliabilityTests::test_a8b_external_cancellation_emits_terminal_record_and_drains \
  tests/test_provider_attempt_reliability.py::ProviderAttemptReliabilityTests::test_a8c_provider_self_cancellation_is_typed_transport_failure \
  tests/test_provider_attempt_reliability.py::ProviderAttemptReliabilityTests::test_p10b_external_cancel_closes_context_manifest_as_cancelled
printf 'Offline replay artifacts: %s\n' "$fixture_root"
