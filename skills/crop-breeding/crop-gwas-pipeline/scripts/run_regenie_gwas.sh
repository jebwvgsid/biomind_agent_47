#!/usr/bin/env bash
set -euo pipefail
cat >&2 <<'EOF'
run_regenie_gwas.sh is a legacy BioMind crop GWAS path.
Crop GWAS V1 routes execution to:
  - crop-gwas-gemma-lmm for GEMMA LMM
  - crop-gwas-gapit3-model-compare for GAPIT3 MLM/MLMM/FarmCPU/BLINK
  - crop-gwas-rmvp-large-scale for rMVP GLM/MLM/FarmCPU
Use the top-level crop-gwas-pipeline router to choose one of those operation skills.
EOF
exit 2
