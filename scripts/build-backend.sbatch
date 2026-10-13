#!/bin/bash
#SBATCH --job-name=biomind-workbench-build
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --time=01:00:00
#SBATCH --export=NONE
#SBATCH --output=/public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/studio/evidence/slurm-backend-%j.log
#SBATCH --error=/public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/studio/evidence/slurm-backend-%j.err
set -euo pipefail
: "${SLURM_JOB_ID:?Submit this build with sbatch}"

# Uses the cluster's default partition and per-CPU memory policy. This script
# builds only. It never sources .env files, starts a service or runs a model.
biomind_build_root='/public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/studio'
biomind_backend_root="$biomind_build_root/backend/pi-studio"
biomind_build_node='/public/home/liusiqi/03.毕设/Biomind_v1/integrations/pi_runtime/.runtime/node/bin/node'
biomind_build_python='/usr/bin/python3'
test -x "$biomind_build_node"
test -x "$biomind_build_python"
test -f "$biomind_backend_root/node_modules/next/dist/bin/next"
cd "$biomind_backend_root"

# A repeated submission cannot write the same production build concurrently.
exec 9>"$biomind_backend_root/.backend-build.lock"
flock -n 9 || { printf '%s\n' 'BACKEND_BUILD_ALREADY_RUNNING' >&2; exit 3; }
printf 'job=%s node=%s source=%s\n' "$SLURM_JOB_ID" "$(hostname)" "$biomind_backend_root"
"$biomind_build_node" --version
biomind_build_adapter_sha=$("$biomind_build_python" -B -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$biomind_backend_root/lib/biomind-session-adapter.ts")

set +e
env -i PATH="$(dirname "$biomind_build_node"):/usr/bin:/bin" \
  NODE_ENV=production PI_WEB_DIST_DIR=.next-pkg NEXT_TELEMETRY_DISABLED=1 \
  NODE_OPTIONS=--max-old-space-size=4096 \
  "$biomind_build_node" "$biomind_backend_root/node_modules/next/dist/bin/next" build --webpack
biomind_build_status=$?
set -e

biomind_bundle_status=2
if [[ "$biomind_build_status" == 0 ]]; then
  if "$biomind_build_node" "$biomind_build_root/scripts/test-compiled-adapter.cjs" \
      --backend-root "$biomind_backend_root" \
      --output "$biomind_build_root/evidence/slurm-backend-$SLURM_JOB_ID-compatibility.json" && \
    "$biomind_build_python" -B "$biomind_build_root/scripts/check-backend-bundle.py" \
      --backend-root "$biomind_backend_root" \
      --compatibility-proof "$biomind_build_root/evidence/slurm-backend-$SLURM_JOB_ID-compatibility.json" \
      --output "$biomind_build_root/evidence/slurm-backend-$SLURM_JOB_ID-bundle.json"; then
    biomind_bundle_status=0
  else
    biomind_bundle_status=$?
  fi
fi

"$biomind_build_python" -B - "$biomind_build_root" "$SLURM_JOB_ID" "$biomind_build_status" "$biomind_bundle_status" "$biomind_build_adapter_sha" <<'PY'
import datetime, hashlib, json, socket, sys
from pathlib import Path
root, job, status = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
bundle_status = int(sys.argv[4])
backend = root / 'backend/pi-studio'
build_id_path = backend / '.next-pkg/BUILD_ID'
adapter_sha = hashlib.sha256((backend / 'lib/biomind-session-adapter.ts').read_bytes()).hexdigest()
source_unchanged = sys.argv[5] == adapter_sha
receipt = {
    'schema': 'BioMindWorkbenchBackendBuildV1',
    'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'jobId': job, 'host': socket.gethostname(), 'exitCode': status,
    'result': 'PASS' if status == 0 and bundle_status == 0 and build_id_path.is_file() and source_unchanged else 'FAIL',
    'compiledBundleCheckExitCode': bundle_status,
    'sourceRoot': str(backend),
    'buildId': build_id_path.read_text().strip() if status == 0 and build_id_path.is_file() else None,
    'adapterSha256': adapter_sha, 'adapterSha256AtStart': sys.argv[5], 'sourceUnchangedDuringBuild': source_unchanged,
    'modelStarted': False, 'serviceStarted': False, 'windowsAcceptance': 'NOT_RUN',
}
target = root / 'evidence' / ('slurm-backend-' + job + '.json')
target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(receipt, ensure_ascii=False))
if not source_unchanged:raise SystemExit(3)
PY
if [[ "$biomind_build_status" != 0 ]]; then exit "$biomind_build_status"; fi
exit "$biomind_bundle_status"
