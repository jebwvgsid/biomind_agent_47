---
name: loop-calling
description: >
  Detect chromatin loops and differential loops from Hi-C, Micro-C, Pore-C, or
  other cooler/mcool contact matrices with Mustache. Use when the user asks for
  loop analysis, loop calling, loop detection, Mustache, differential loops,
  loop distance statistics, or multi-sample loop summaries from .cool/.mcool
  matrices. Do not use for annotating loop anchors with histone marks; use
  loop-classification after loop calls exist.
legacy_ids: [loops-analysis, mustache-loop-calling, differential-loops, loop-detection, Loops分析]
compatibility: Requires mustache on PATH or an environment that provides the mustache Python package and CLI.
---

# Loop Calling With Mustache

Use this skill to call chromatin loops and differential loops with `mustache` from high-resolution Hi-C or Micro-C contact matrices.

Mustache is most appropriate for high-resolution contact maps, commonly 10 kb down to 1 kb or finer when sequencing depth supports it. Report the matrix resolution and normalization/balancing status whenever presenting results.

## When To Use

- User asks for `Loops 分析`, loop calling, chromatin loop detection, Mustache, or `.tsv` loop outputs.
- User provides one or more `.cool` / `.mcool` matrices and a resolution.
- User wants pairwise differential loop calls such as `leaf_vs_bud.diffloop1`, `.diffloop2`, `.loop1`, or `.loop2`.
- User wants basic loop statistics such as counts and genomic distance distributions.

Do not use this skill for loop anchor annotation with H3K27ac/H3K4me1/H3K4me3 or other BED tracks; use `skills/3d-dna/loop-classification` after loop calling.

## Inputs

Minimum inputs:

- Contact matrix path for each sample.
- Resolution in bp, for example `10000`, `5000`, or `1000`.
- Output root, usually `Loops/Mustache`.

Optional inputs:

- Sample sheet with columns `sample` and `matrix`.
- Pair sheet with columns `sample1`, `sample2`, and optional `prefix`.
- Mustache thresholds: `-pt` p/FDR threshold and `-st` sparsity threshold.
- CPU processes.

## Quick Start

Single or small batch:

```bash
python skills/3d-dna/loop-calling/scripts/run_mustache_loops.py \
  --sample leaf Matrix/Final/leaf.mcool \
  --sample bud Matrix/Final/bud.mcool \
  --resolution-bp 10000 \
  --out-root Loops/Mustache \
  --processes 6 \
  --pt 0.05 \
  --st 0.88 \
  --diff leaf bud
```

Sample sheet:

```bash
python skills/3d-dna/loop-calling/scripts/run_mustache_loops.py \
  --sample-sheet skills/3d-dna/loop-calling/examples/samples.tsv \
  --pair-sheet skills/3d-dna/loop-calling/examples/pairs.tsv \
  --resolution-bp 10000 \
  --out-root Loops/Mustache \
  --processes 6
```

Only summarize existing Mustache outputs:

```bash
python skills/3d-dna/loop-calling/scripts/run_mustache_loops.py \
  --sample leaf Matrix/Final/leaf.mcool \
  --sample bud Matrix/Final/bud.mcool \
  --resolution-bp 10000 \
  --out-root Loops/Mustache \
  --stats-only
```

## Parameter Guidance

| Situation | Suggested parameters |
|---|---|
| Standard 10 kb loop calling | `--pt 0.05 --st 0.88` |
| Sparse dataset | lower sparsity threshold, for example `--st 0.7` |
| Very high resolution, such as 1 kb | use `--st 0.7` and consider relaxed `--pt 0.1` |
| Many samples | use `--sample-sheet` instead of hard-coded names |
| Pairwise differential loops | provide `--diff sampleA sampleB` or `--pair-sheet` |

Keep all samples in one comparison at the same resolution. Loop counts and loop distances are resolution-dependent.

## Outputs

For `--out-root Loops/Mustache --resolution-bp 10000`:

```text
Loops/Mustache/10000/
├── leaf.tsv
├── bud.tsv
├── leaf_vs_bud.diffloop1
├── leaf_vs_bud.diffloop2
├── leaf_vs_bud.loop1
├── leaf_vs_bud.loop2
├── loop_run_summary.tsv
├── loop_stats.tsv
└── loop_distances.tsv
```

Mustache loop tables usually contain:

```text
BIN1_CHR  BIN1_START  BIN1_END  BIN2_CHROMOSOME  BIN2_START  BIN2_END  FDR  DETECTION_SCALE
```

The wrapper adds summary tables without changing Mustache native files.

## Workflow

1. Verify `mustache --help` works in the active environment.
2. Confirm `.mcool` resolution explicitly. Do not guess a resolution from common values.
3. Run loop calling for each sample.
4. If requested, run pairwise differential analysis with `diff_mustache.py`.
5. Summarize loop count, distance, FDR, and detection scale across native Mustache outputs.
6. Use `loop-classification` only after loop calls exist and BED mark files are available.

## Common Commands

Native Mustache loop detection:

```bash
mustache --file Matrix/Final/leaf.mcool \
  --resolution 10000 \
  --outfile Loops/Mustache/10000/leaf.tsv \
  --processes 6 \
  -pt 0.05 \
  -st 0.88
```

Native differential loop analysis:

```bash
python /path/to/site-packages/mustache/diff_mustache.py \
  -f1 Matrix/Final/leaf.mcool \
  -f2 Matrix/Final/bud.mcool \
  --resolution 10000 \
  --outfile Loops/Mustache/10000/leaf_vs_bud \
  --processes 6 \
  -pt 0.05 \
  -pt2 0.05 \
  -st 0.88
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `mustache not found` | Environment missing Mustache CLI | Activate/install the Mustache environment, then rerun `mustache --help` |
| `diff_mustache.py not found` | Mustache installed without script on PATH or different Python environment | Pass `--diff-script /path/to/diff_mustache.py` |
| Very few loops | Data sparse, resolution too fine, or threshold too strict | Try lower resolution or `--st 0.7`; for very high resolution consider `--pt 0.1` |
| Very slow at 1 kb | High-resolution genome-wide loop calling is expensive | Test selected chromosomes or raise resolution for exploratory analysis |
| Pairwise output hard to interpret | Direction unclear | Name output prefixes as `sample1_vs_sample2` and report which sample is `f1` / `f2` |

## Related Skills

- `skills/3d-dna/loop-classification` - classify loop anchors using histone marks or other BED annotations.
- `skills/3d-dna/tad-calling` - TAD calling from contact matrices.
- `skills/3d-dna/differential-tad` - target/control differential TAD testing.
