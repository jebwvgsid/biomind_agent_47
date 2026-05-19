#!/usr/bin/env bash
set -euo pipefail

python skills/3d-dna/loop-classification/scripts/classify_loops_by_marks.py \
  --loops Loops/Mustache/10000/leaf.tsv \
  --mark-sheet skills/3d-dna/loop-classification/examples/marks.tsv \
  --out-dir Loops/Classification/10000/leaf \
  --fraction-target mark \
  --overlap-fraction 0.5
