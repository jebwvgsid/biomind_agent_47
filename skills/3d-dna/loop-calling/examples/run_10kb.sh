#!/usr/bin/env bash
set -euo pipefail
SKILL_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

python "${SKILL_DIR}/scripts/run_mustache_loops.py" \
  --sample-sheet "${SKILL_DIR}/examples/samples.tsv" \
  --pair-sheet "${SKILL_DIR}/examples/pairs.tsv" \
  --resolution-bp 10000 \
  --out-root Loops/Mustache \
  --processes 6 \
  --pt 0.05 \
  --st 0.88
