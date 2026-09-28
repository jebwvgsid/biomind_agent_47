#!/usr/bin/env bash
set -euo pipefail
SKILL_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

python "${SKILL_DIR}/scripts/classify_loops_by_marks.py" \
  --loops Loops/Mustache/10000/leaf.tsv \
  --mark-sheet "${SKILL_DIR}/examples/marks.tsv" \
  --out-dir Loops/Classification/10000/leaf \
  --fraction-target mark \
  --overlap-fraction 0.5
