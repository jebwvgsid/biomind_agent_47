#!/usr/bin/env bash
set -euo pipefail

python skills/3d-dna/loop-calling/scripts/run_mustache_loops.py \
  --sample-sheet skills/3d-dna/loop-calling/examples/samples.tsv \
  --pair-sheet skills/3d-dna/loop-calling/examples/pairs.tsv \
  --resolution-bp 10000 \
  --out-root Loops/Mustache \
  --processes 6 \
  --pt 0.05 \
  --st 0.88
