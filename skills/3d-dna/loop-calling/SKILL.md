---
name: loop-calling
description: 'Detect chromatin loops and differential loops from Hi-C, Micro-C, Pore-C, or other cooler/mcool contact
  matrices with Mustache. Use when the user asks for loop analysis, loop calling, loop detection, Mustache, differential
  loops, loop distance statistics, or multi-sample loop summaries from .cool/.mcool matrices. Do not use for annotating
  loop anchors with histone marks; use loop-classification after loop calls exist. For read-only balance preflight,
  inspect the cooler bins `weight` column on `mcool::/resolutions/{resolution}`; normal loop-calling runs may auto-balance
  missing or unusable weights through the wrapper.

  '
compatibility: 需要本技能 references/dependencies.md 列明的现有软件；不自动安装。输入输出位于分析项目。
metadata:
  version: 1.1.0
---


## 资源定位与依赖

使用当前 Agent 的标准技能发现与文件读取能力加载本文件。`scripts/`、`references/`、
`assets/` 相对本 SKILL.md 的实际目录；输入、输出和样本清单相对本次分析项目。
命令中的 `${SKILL_DIR}` 必须先设为实际技能目录的绝对路径；该变量不由宿主自动提供。
例如，从已加载位置解析目录后，以带引号的脚本绝对路径执行，保持工作目录为分析项目，
不要在技能安装目录写结果。跨技能资源按其实际目录定位；分享完整技能包时保留目录结构。
软件和参考数据库来自已有环境或用户显式提供的位置。依赖见
[dependencies.md](references/dependencies.md)；安装、升级或修改环境需要事先明确授权。
本文命令可由任意提供命令执行能力的 Agent 或终端使用；不会在读取技能时安装或体检环境。

# Loop Calling With Mustache

Use this skill to call chromatin loops and differential loops with ay-lab MUSTACHE from high-resolution Hi-C or Micro-C contact matrices.

Dependency guard: verify the selected ay-lab entrypoint with its real `--help`
exit status. Installed modules use `python -m mustache --help`; an existing
source checkout can use `python /absolute/path/to/mustache.py --help` in the
same environment. A missing module does not prove the environment or source
checkout is absent. For source checkouts pass `--mustache-script` to the wrapper;
it also finds the adjacent `diff_mustache.py`, or accepts `--diff-script`.
Do not install the unrelated PyPI package named `mustache`. The supported
installation path is cloning `https://github.com/ay-lab/mustache` and creating
the conda environment from `./mustache/environment.yml`.

Use registered source paths when available; machine-specific paths belong in
the runtime registry, not this skill. In a protected command, select the same
environment for each diagnostic and declare external source files through
`read_paths` (both `mustache.py` and `diff_mustache.py` for a source-based pair).
An unmounted path is not evidence that software is absent on the host.
If no usable entrypoint is available, follow the task's blocked/installation
policy and write any requested report and reproduction files. Do not replace
the selected environment with the BioMind host environment.

To check both entrypoints without reading matrices or writing outputs:

```bash
python "${SKILL_DIR}/scripts/run_mustache_loops.py" \
  --mustache-script /absolute/path/to/mustache.py \
  --diff-script /absolute/path/to/diff_mustache.py --runtime-check-only
```

Keep the diagnostic's exit status: do not pipe it to `head`/`tail` and report
that filter's `$?` as Mustache's result. The wrapper preserves the subprocess
status and performs this check before normal analysis, including auto-balancing.

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
python "${SKILL_DIR}/scripts/run_mustache_loops.py" \
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
python "${SKILL_DIR}/scripts/run_mustache_loops.py" \
  --sample-sheet "${SKILL_DIR}/examples/samples.tsv" \
  --pair-sheet "${SKILL_DIR}/examples/pairs.tsv" \
  --resolution-bp 10000 \
  --out-root Loops/Mustache \
  --processes 6
```

Only summarize existing Mustache outputs:

```bash
python "${SKILL_DIR}/scripts/run_mustache_loops.py" \
  --sample leaf Matrix/Final/leaf.mcool \
  --sample bud Matrix/Final/bud.mcool \
  --resolution-bp 10000 \
  --out-root Loops/Mustache \
  --stats-only
```

Read-only preflight:

```bash
python "${SKILL_DIR}/scripts/run_mustache_loops.py" \
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

For `.mcool` preflight and balancing with cooler, use the full cooler URI:

```text
sample.mcool::/resolutions/10000
```

Do not use `sample.mcool::10000`; that is not the canonical mcool group path.
The ay-lab command line takes the `.mcool` file path plus `-r 10000` and builds
the group URI internally. The wrapper converts the matching URI for native
Mustache/diff commands while retaining the full URI for cooler and reports.

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
python - <<'PY'
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

Preferred read-only preflight:

```bash
python "${SKILL_DIR}/scripts/run_mustache_loops.py" \
  --sample leaf Matrix/Final/leaf.mcool \
  --sample bud Matrix/Final/bud.mcool \
  --resolution-bp 10000 \
  --out-root Loops/Mustache \
  --preflight-only
```

If any row in `loop_preflight.tsv` has `balanced=no`, report
that fact before execution. The normal wrapper command remains available because
the execution step can auto-balance before loop calling.

## Workflow

1. Confirm `.mcool` resolution explicitly and build `::/resolutions/{resolution}` URIs.
2. Run `--preflight-only` to check the cooler `weight` bin column for finite values when a read-only inspection step is needed.
3. If requested samples fail preflight, plan to use the normal wrapper auto-balance behavior unless the user disabled balancing.
4. Verify the selected module, executable, or source entrypoint with `--help`; check the differential entrypoint too when requested. `--runtime-check-only` uses the same entrypoints as analysis.
5. Run loop calling with the wrapper default auto-balance enabled, unless the user explicitly requests `--no-auto-balance` or `--skip-balance-check`.
6. If requested, run pairwise differential analysis with `diff_mustache.py`.
7. Summarize loop count, distance, FDR, and detection scale across native Mustache outputs.
8. Use `loop-classification` only after loop calls exist and BED mark files are available.

## Common Commands

Native Mustache loop detection:

```bash
python -m mustache \
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
| `mustache not found` or `No module named mustache` | The ay-lab MUSTACHE conda environment is missing or not used | Clone ay-lab MUSTACHE, run `conda env create -f ./mustache/environment.yml`, then rerun `python -m mustache --help` |
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
