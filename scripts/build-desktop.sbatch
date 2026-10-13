#!/bin/bash
#SBATCH --job-name=biomind-desktop-build
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --time=00:30:00
#SBATCH --export=NONE
#SBATCH --output=/public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/studio/evidence/slurm-desktop-%j.log
#SBATCH --error=/public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/studio/evidence/slurm-desktop-%j.err
set -euo pipefail
: "${SLURM_JOB_ID:?Submit this build with sbatch}"
biomind_build_root='/public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/studio'
biomind_build_node='/public/home/liusiqi/03.毕设/Biomind_v1/integrations/pi_runtime/.runtime/node/bin/node'
cd "$biomind_build_root/desktop"
exec 9>.desktop-build.lock
flock -n 9 || exit 3
"$biomind_build_node" --version
# Builds desktop only, no server restart, model call, credential or shell profile.
env -i PATH="$(dirname "$biomind_build_node"):/usr/bin:/bin" \
  NODE_OPTIONS=--max-old-space-size=4096 GOMAXPROCS=4 UV_THREADPOOL_SIZE=4 \
  "$biomind_build_node" node_modules/electron-vite/bin/electron-vite.js build
