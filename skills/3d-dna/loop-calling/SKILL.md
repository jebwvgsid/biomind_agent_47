---
name: loop-calling
description: >
  Detect chromatin loops and differential loops from Hi-C, Micro-C, Pore-C, or
  other cooler/mcool contact matrices with Mustache. Use when the user asks for
  loop analysis, loop calling, loop detection, Mustache, differential loops,
  loop distance statistics, or multi-sample loop summaries from .cool/.mcool
  matrices. Do not use for annotating loop anchors with histone marks; use
  loop-classification after loop calls exist. For read-only balance preflight,
  inspect the cooler bins `weight` column on `mcool::/resolutions/{resolution}`;
  normal loop-calling runs may auto-balance missing or unusable weights through
  the wrapper.
legacy_ids: [loops-analysis, mustache-loop-calling, differential-loops, loop-detection, Loops分析, loop 鉴定, loop鉴定, loop 分析, loop分析]
compatibility: Requires ay-lab MUSTACHE; do not use the unrelated PyPI package named `mustache`.
metadata:
  version: "1.1.0"
  tags: [loop, loops, loop-calling, loop-detection, chromatin-loop, chromatin-loops, Mustache, Hi-C, Micro-C, Pore-C, mcool, cool, loop鉴定, loop分析]
  task_types: [chromatin_contact_loop]
  stage_tags: [loop_calling]
  inputs: [cool_matrix, mcool_matrix, resolution]
  outputs: [loop_calls, loop_summary, differential_loop_calls]
  when_to_use:
    - Workflow stage is loop_calling.
    - User asks to call, detect, or identify chromatin loops from contact matrices.
    - User asks for Mustache loop outputs or differential loops.
  when_not_to_use:
    - Loop anchor classification after loop calls already exist.
    - A/B compartment calling or compartment transition analysis.
    - TAD calling, TAD boundary comparison, or differential TAD testing.
runtime:
  default_env: mustache
  env_runner: conda run -n mustache python
  language: python
  packages: [mustache]
  capabilities: [loop-calling, differential-loops, chromatin-loops]
  healthcheck: conda run -n mustache python -m mustache --help
  install_policy: declared-only
  install_commands:
    - if [ -d mustache/.git ]; then echo "[OK] ay-lab mustache repo exists"; else git clone https://github.com/ay-lab/mustache mustache; fi
    - conda env create -f ./mustache/environment.yml
  install_fallbacks:
    - conda env update -n mustache -f ./mustache/environment.yml
---

# Loop Calling With Mustache

Use this skill to call chromatin loops and differential loops with ay-lab MUSTACHE from high-resolution Hi-C or Micro-C contact matrices.

Dependency guard: the executable must pass `conda run -n mustache python -m mustache --help`.
Do not install the unrelated PyPI package named `mustache`. The supported
installation path is cloning `https://github.com/ay-lab/mustache` and creating
the conda environment from `./mustache/environment.yml`.

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
- Cooler balance weight column name, default `weight`.

## Quick Start

Single or small batch:

```bash
conda run -n mustache python skills/3d-dna/loop-calling/scripts/run_mustache_loops.py \
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
conda run -n mustache python skills/3d-dna/loop-calling/scripts/run_mustache_loops.py \
  --sample-sheet skills/3d-dna/loop-calling/examples/samples.tsv \
  --pair-sheet skills/3d-dna/loop-calling/examples/pairs.tsv \
  --resolution-bp 10000 \
  --out-root Loops/Mustache \
  --processes 6
```

Only summarize existing Mustache outputs:

```bash
conda run -n mustache python skills/3d-dna/loop-calling/scripts/run_mustache_loops.py \
  --sample leaf Matrix/Final/leaf.mcool \
  --sample bud Matrix/Final/bud.mcool \
  --resolution-bp 10000 \
  --out-root Loops/Mustache \
  --stats-only
```

Consultant-safe preflight only:

```bash
python skills/3d-dna/loop-calling/scripts/run_mustache_loops.py \
  --sample leaf Matrix/Final/leaf.mcool \
  --sample bud Matrix/Final/bud.mcool \
  --resolution-bp 10000 \
  --out-root Loops/Mustache \
  --preflight-only
```

This command only opens the effective cooler URIs and writes
`loop_preflight.tsv`; it does not run Mustache, install software, or balance
matrices.

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

## Mandatory Preflight

For `.mcool` inputs, always use the full cooler URI:

```text
sample.mcool::/resolutions/10000
```

Do not use `sample.mcool::10000`; that is not the canonical mcool group path.

Balance must be verified by opening the effective cooler URI and checking that
`clr.bins()` contains a finite `weight` column. Do not use `cooler balance
--stdout` to decide whether an input already has valid weights; that command can
attempt balancing/calculation and is not a reliable pre-existing weight check.

The wrapper checks balance by default. During normal loop calling, if `weight`
is missing or has no finite values, the wrapper runs `cooler balance` on the
effective URI and re-checks before calling Mustache. During `--preflight-only`,
the wrapper only reports balance status and never modifies the matrix. Use
`--no-auto-balance` when the user explicitly wants missing weights to stop the
analysis instead of being repaired.

Manual check:

```bash
conda run -n mustache python - <<'PY'
import cooler
uri = "Matrix/Final/leaf.mcool::/resolutions/10000"
clr = cooler.Cooler(uri)
print(list(clr.bins().columns))
w = clr.bins()["weight"][:]
print(w.notna().sum(), len(w))
PY
```

Use `--skip-balance-check` only when the user explicitly confirms that
correction was verified outside cooler's `weight` column.

Preferred Consultant preflight:

```bash
python skills/3d-dna/loop-calling/scripts/run_mustache_loops.py \
  --sample leaf Matrix/Final/leaf.mcool \
  --sample bud Matrix/Final/bud.mcool \
  --resolution-bp 10000 \
  --out-root Loops/Mustache \
  --preflight-only
```

If any row in `loop_preflight.tsv` has `balanced=no`, the Consultant must report
that fact to Planner. Planner may still use the normal wrapper command because
the execution step can auto-balance before loop calling.

## Workflow

1. Confirm `.mcool` resolution explicitly and build `::/resolutions/{resolution}` URIs.
2. Run `--preflight-only` to check the cooler `weight` bin column for finite values when a read-only inspection step is needed.
3. If requested samples fail preflight, plan to use the normal wrapper auto-balance behavior unless the user disabled balancing.
4. Verify `conda run -n mustache python -m mustache --help` only after inputs are valid. This is the required runtime healthcheck.
5. Run loop calling with the wrapper default auto-balance enabled, unless the user explicitly requests `--no-auto-balance` or `--skip-balance-check`.
6. If requested, run pairwise differential analysis with `diff_mustache.py`.
7. Summarize loop count, distance, FDR, and detection scale across native Mustache outputs.
8. Use `loop-classification` only after loop calls exist and BED mark files are available.

## Common Commands

Native Mustache loop detection:

```bash
conda run -n mustache python -m mustache \
  -f Matrix/Final/leaf.mcool \
  -r 10000 \
  -o Loops/Mustache/10000/leaf.tsv \
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
| `mustache not found` or `No module named mustache` | The ay-lab MUSTACHE conda environment is missing or not used | Clone ay-lab MUSTACHE, run `conda env create -f ./mustache/environment.yml`, then rerun `conda run -n mustache python -m mustache --help` |
| `pip install mustache` succeeds but CLI still fails | Wrong package; PyPI `mustache` is unrelated to the Hi-C loop caller | Remove/ignore that package and install from the ay-lab GitHub environment; do not treat `pip install mustache` as a valid fix |
| `mcool::10000` fails or reports unbalanced | Wrong cooler URI for mcool resolution | Use `mcool::/resolutions/10000` |
| `cooler balance --stdout` reports not balanced | The command is not a valid existing-weight check | Inspect `clr.bins()["weight"]` on the effective URI |
| `diff_mustache.py not found` | Mustache installed without script on PATH or different Python environment | Pass `--diff-script /path/to/diff_mustache.py` |
| Very few loops | Data sparse, resolution too fine, or threshold too strict | Try lower resolution or `--st 0.7`; for very high resolution consider `--pt 0.1` |
| Very slow at 1 kb | High-resolution genome-wide loop calling is expensive | Test selected chromosomes or raise resolution for exploratory analysis |
| Pairwise output hard to interpret | Direction unclear | Name output prefixes as `sample1_vs_sample2` and report which sample is `f1` / `f2` |

## Related Skills

- `skills/3d-dna/loop-classification` - classify loop anchors using histone marks or other BED annotations.
- `skills/3d-dna/tad-calling` - TAD calling from contact matrices.
- `skills/3d-dna/differential-tad` - target/control differential TAD testing.
