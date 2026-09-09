---
name: tad-analysis
description: >
  Run generalized TAD analysis / TAD calling with HiCExplorer hicFindTADs for
  one or many Hi-C, Micro-C, Pore-C, or chromatin contact matrix samples. Use
  when the user asks for TAD分析, TAD calling, hicFindTADs, HiCExplorer, 拓扑关联结构域,
  TAD边界, TAD domains, separation score, domain length statistics, multi-sample
  TAD batch analysis, sample sheet workflows, resolution confirmation, or balance
  checks for .cool/.mcool matrices.
version: "1.2.0"
legacy_ids: [genomics/chromatin/tad-calling, tad-calling, tad-analysis]
tags: [TAD, TAD分析, TAD-calling, hicFindTADs, HiCExplorer, Hi-C, Micro-C, Pore-C, mcool, cool, cooler-balance, sample-sheet, multi-sample, resolution, boundaries, domains]
task_types: [chromatin_contact_tad]
stage_tags: [matrix_preflight, tad_calling]
inputs: [validated_matrix_inputs, cool_matrix, mcool_matrix, sample_sheet, resolution]
outputs: [tad_domains, tad_boundaries, tad_summary, matrix_balance_report]
when_to_use:
  - Workflow stage is tad_calling.
  - User needs hicFindTADs TAD calling from .cool or .mcool matrices.
when_not_to_use: [existing TAD boundary comparison only]
runtime:
  default_env: hicexplore
  env_runner: conda run -n hicexplore python
  language: python
  packages: [hicexplorer, cooler, matplotlib]
  capabilities: [tad-calling, hicfindtads, cooler-preflight]
  healthcheck: conda run -n hicexplore python -c "import shutil, cooler, matplotlib; assert shutil.which('hicFindTADs'); print('OK')"
---

# TAD Analysis with hicFindTADs

## Overview

This skill runs HiCExplorer `hicFindTADs` on one or many `.cool/.mcool` contact matrices, after confirming the resolution and ensuring that the cooler has usable balance weights.
It writes native HiCExplorer outputs plus combined multi-sample tables for domains, boundaries, and domain length statistics.

| Output group | Files |
|---|---|
| HiCExplorer native outputs | `{sample}_boundaries.bed`, `{sample}_boundaries.gff`, `{sample}_domains.bed`, `{sample}_score.bedgraph`, `{sample}_tad_score.bm` |
| Combined summary outputs | `tad_domain_stats.tsv`, `tad_domains_long.tsv`, `tad_boundaries_long.tsv`, `tad_run_summary.tsv` |
| Optional figure | `tad_domain_lengths.png` |
| Read-only matrix balance preflight | `mcool_balance_preflight.tsv`, `stage_status.tsv`, `report.md`, `reproduce.sh` |

## When to Use This Skill

- User asks for "TAD分析", "TAD边界", "TAD位置", "拓扑关联结构域", "隔离分数", or "多样本TAD统计".
- User mentions `hicFindTADs`, `HiCExplorer`, `--correctForMultipleTesting fdr`, `_domains.bed`, `_boundaries.bed`, or `_score.bedgraph`.
- User provides one or many `.cool`, `.mcool`, or `.mcool::/resolutions/N` contact matrices.
- User wants to run TAD calling across many samples using a sample sheet instead of hard-coded leaf/bud names.
- User asks to verify or ensure the input cooler has been balanced/corrected before TAD calling.
- User only asks to check whether `.mcool` files are balanced and which resolutions can be used directly.
- User has not provided a resolution and needs the Agent to ask for/confirm the intended resolution before running `.mcool` data.

## Quick Start

```bash
conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py \
  --sample-sheet samples.tsv \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer \
  --correct-for-multiple-testing fdr
```

Example `samples.tsv`:

```tsv
sample	matrix
leaf	hic_matrix/Corrected/leaf.mcool
bud	hic_matrix/Corrected/bud.mcool
root	hic_matrix/Corrected/root.mcool
flower	hic_matrix/Corrected/flower.mcool
```

## Software Environment

Use the shared TAD/HiCExplorer environment `hicexplore` for TAD calling, differential TAD, boundary statistics, and Hi-C heatmap-related skills. Do not emit direct package-manager installation commands as an execution step.

Verify first. If this succeeds, do not install anything:

```bash
conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py --help
```

If verification fails because the environment is missing or HiCExplorer, cooler, or matplotlib is missing, delegate installation into the same shared environment with these tags instead of writing a conda command:

```text
<install_tool env="hicexplore">hicexplorer</install_tool>
<install_tool env="hicexplore">cooler</install_tool>
<install_tool env="hicexplore">matplotlib</install_tool>
```

After the install subagent completes, re-run the verification command before running data. Never create a second TAD environment unless the user explicitly asks for a different environment name.

## Agent Decision Protocol

### Resolution-only validation (Generate action contract)

For a step that only checks input resolution availability, use the registered
`scripts/check_cooler_resolution.py` resource. Declare
`skill_resources="3d-dna/tad-calling#scripts/check_cooler_resolution.py"` on the
script block and use its binder-provided environment variable as the script path.
Run one `conda run --no-capture-output -n hicexplore python -u` process for all
inputs, with repeated `--input <approved-resource-handle> <approved-bound-path>`,
`--resolution-bp <approved-positive-integer>`, and
`--observation <exact-current-declared-observation-handle>`.
The same query result supplies display, exact group comparison, and the existing
structured observation emitter. Do not rewrite this query as inline Python,
compare integers to group paths, or query again for the decision. A negative
result exits nonzero while preserving the negative observation. Query errors do
not mean the resolution is absent. Metadata presence does not validate the full
matrix or its balance. This entry neither balances nor writes inputs.

### Mandatory preflight — confirm resolution and balance

Before running `hicFindTADs`, do these checks:

1. Verify the `hicexplore` environment. If dependencies are missing, use `<install_tool env="hicexplore">...</install_tool>` and re-verify.
2. If the user provides plain `.mcool` paths and no explicit resolution, ask the user which resolution to use. Do not guess from common values like 40000.
3. If paths are full URIs such as `sample.mcool::/resolutions/40000`, treat that as explicit resolution confirmation.
4. Prefer one confirmed resolution per multi-sample comparison. Do not combine different resolutions in one summary unless the user explicitly asks for separate runs.
5. Let the script ensure cooler balance by default. It verifies the `weight` bin column before running `hicFindTADs`; if the column is missing or has no finite values, it automatically runs `cooler balance` on the effective cooler URI and then re-checks. For a read-only preflight step, run the wrapper with `--preflight-only`.
6. Use `--skip-balance-check` only when the user says the matrix is already corrected by another method and the `weight` column check is inappropriate.

Do not write ad hoc balance checks with `clr.bins()["weight"].dropna()` or
`clr.bins()["weight"].notna()`: `clr.bins()["weight"]` is a Cooler selector,
not a pandas Series, until it is sliced. Do not use `cooler attrs | grep weight`
to decide whether balancing weights exist; `weight` is a bins-table column, not
a top-level cooler attribute. Prefer the wrapper script's built-in balance check.
If a manual check is required, use `weights = clr.bins()["weight"][:]` before
calling pandas-like methods.

For a read-only task that asks only whether `.mcool` files are already balanced
and which resolutions are directly usable, use the dedicated balance preflight
script. Do not use `cooler.fileops.list_coolers()` alone as a balance check; it
only lists available resolution groups.

```bash
conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/check_mcool_balance.py \
  --sample leaf /path/to/leaf.mcool \
  --sample bud /path/to/bud.mcool \
  --out-dir /path/to/results/BM-3DDNA-BALANCE-MEDIUM-002
```

The script is read-only. It reports `weight` column presence, total bin count,
finite weight count, finite positive weight count, finite ratio, and usable
status for every sample-resolution pair. A resolution is usable when the bins
table contains the weight column, at least one finite weight exists, and at
least one finite weight is >0. Small numbers of NaN weights are reported but do
not automatically mean the matrix is unbalanced. Always base `report.md` on
`mcool_balance_preflight.tsv`; the report summary must not contradict the table.

Read-only wrapper preflight command:

```bash
conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py --sample leaf /path/to/leaf.mcool --sample bud /path/to/bud.mcool --resolution-bp 40000 --out-root TADs/HiCExplorer --preflight-only
```

Resolution discovery command when the user has not specified a resolution:

```bash
conda run -n hicexplore python -c "import cooler; print(cooler.fileops.list_coolers('/path/to/sample.mcool'))"
```

Do not use bare commands such as `cooler info sample.mcool` unless `which cooler` has been checked and does not point to a stale `bin_shims/cooler`. Prefer `conda run -n hicexplore cooler ...` or `conda run -n hicexplore python -c "import cooler; ..."` for HiCExplorer workflows.

### Tier 1 — 直接调用脚本

**触发条件**：标准多样本或单样本 hicFindTADs TAD 分析；用户已经确认样本路径、输出目录、分辨率，并允许默认 balance handling。

For many samples, use a sample sheet:

```bash
conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py \
  --sample-sheet /path/to/samples.tsv \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer \
  --correct-for-multiple-testing fdr
```

For a small number of samples, repeat `--sample`:

```bash
conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py \
  --sample SAMPLE_A /path/to/sample_a.mcool \
  --sample SAMPLE_B /path/to/sample_b.mcool \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer \
  --correct-for-multiple-testing fdr
```

### Tier 2 — 只改参数

**触发条件**：用户只想调整样本输入方式、分辨率、统计模式、线程数或 hicFindTADs 常用参数，分析逻辑不变。

| 用户可能说什么 | 对应加哪个参数 |
|---|---|
| "很多样本 / 批量样本表" | `--sample-sheet samples.tsv` |
| "用 40kb 分辨率" / "40000 resolution" | `--resolution-bp 40000` |
| "已经是 full URI 了" | `--sample S sample.mcool::/resolutions/40000` |
| "多线程 / 32 线程" | `--number-of-processors 32` |
| "只统计已有结果，不重新跑" | `--stats-only` |
| "结果已存在就跳过运行" | `--skip-existing` |
| "重新覆盖已有结果" | `--force` |
| "不要自动 balance，缺 weight 就报错" | `--no-auto-balance` |
| "多重检验用 fdr" | `--correct-for-multiple-testing fdr` |
| "限制染色体 Chr03" | `--chromosome Chr03` |
| "这个矩阵没有 weight 但我确认已校正" | `--skip-balance-check` |
| "加 hicFindTADs 原生命令参数" | `--extra-arg=--minDepth --extra-arg=80000` |
| "画 domains 长度分布" | `--plot-lengths` |

Do NOT touch the script for these changes.

### Tier 3 — 读脚本并修改

**触发条件**：用户需要新增统计列、改变输出 schema、合并基因注释、比较两个样本 TAD 重叠、改图形类型，或支持非 HiCExplorer 格式。

触发关键词：

- 中文：`新增统计`, `合并注释`, `比较重叠`, `改输出列`, `改图`, `换成箱线图`, `只保留某类TAD`, `计算差异TAD`
- English: `custom stats`, `merge annotation`, `overlap TADs`, `change columns`, `boxplot`, `differential TAD`, `custom parser`

操作步骤（Linux/POSIX shell）：

```bash
# 1. Read the script first
cat skills/3d-dna/tad-calling/scripts/call_tads.py

# 2. Copy to a working file. NEVER edit files inside the skill directory.
cp skills/3d-dna/tad-calling/scripts/call_tads.py ./call_tads_custom.py

# 3. Modify only the copied script, then run it.
conda run -n hicexplore python ./call_tads_custom.py --sample-sheet samples.tsv --resolution-bp 40000 --out-root TADs/HiCExplorer
```

NEVER edit files inside the skill directory unless the user explicitly asks to update the skill package.

## Core Concepts

`hicFindTADs` identifies TADs from a corrected contact matrix by calculating a TAD-separation score along the genome. Local minima in this score indicate boundaries where contacts across the boundary are depleted. Adjacent boundaries define TAD domains, while `_score.bedgraph` stores the genome-wide separation score track.

Key parameters:

- `--matrix`: HiCExplorer contact matrix path. For multi-resolution cooler files, use `sample.mcool::/resolutions/40000` or pass plain `.mcool` plus `--resolution-bp 40000`.
- `--outPrefix`: Prefix for native outputs. This skill builds `{out-root}/{resolution}/{sample}` so outputs become `{sample}_domains.bed`, `{sample}_boundaries.bed`, etc.
- `--correctForMultipleTesting`: Multiple-testing correction method used by hicFindTADs; `fdr` matches the reference workflow.
- `--numberOfProcessors`: CPU workers. More workers can reduce runtime but increase memory pressure.
- `--weight-name`: cooler balance column checked before running. Default is `weight`.
- `--sample-sheet`: TSV/CSV manifest for arbitrary sample counts. Required columns are `sample` and `matrix`; optional column is `resolution_bp`.

Input format requirements and traps:

- `.mcool` files usually contain multiple resolutions. The Agent must confirm one resolution with the user before running.
- Multi-sample comparison should use the same resolution across samples; otherwise TAD counts and sizes are not directly comparable.
- The matrix should be balanced/corrected. The script checks for finite values in the cooler `weight` bin column by default.
- Sample labels become output prefixes. Avoid duplicate labels after punctuation is sanitized.
- HiCExplorer output files are whitespace/tab-delimited and may start with comment header lines beginning with `#`.

## Common Patterns

Many samples from a manifest:

```bash
conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py \
  --sample-sheet samples.tsv \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer \
  --correct-for-multiple-testing fdr
```

Small batch using repeated `--sample`:

```bash
conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py \
  --sample leaf hic_matrix/Corrected/leaf.mcool \
  --sample bud hic_matrix/Corrected/bud.mcool \
  --sample root hic_matrix/Corrected/root.mcool \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer
```

Full URI input where resolution is encoded in every matrix path:

```bash
conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py \
  --sample sampleA hic_matrix/Corrected/sampleA.mcool::/resolutions/40000 \
  --sample sampleB hic_matrix/Corrected/sampleB.mcool::/resolutions/40000 \
  --out-root TADs/HiCExplorer
```

Generate only statistics from existing HiCExplorer outputs:

```bash
conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py \
  --sample-sheet samples.tsv \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer \
  --stats-only
```

Run when balance has been externally verified but no `weight` column exists:

```bash
conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py \
  --sample-sheet samples.tsv \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer \
  --skip-balance-check
```

Pass advanced hicFindTADs options without editing the script:

```bash
conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py \
  --sample-sheet samples.tsv \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer \
  --extra-arg=--minDepth \
  --extra-arg=80000 \
  --extra-arg=--maxDepth \
  --extra-arg=2000000
```

## Using the Core API

HiCExplorer `hicFindTADs` is primarily a command-line workflow, not a Python API workflow. For custom wrappers, use subprocess and explicit parsers rather than importing private HiCExplorer internals:

```python
import subprocess

cmd = [
    "hicFindTADs",
    "--matrix", "hic_matrix/Corrected/sampleA.mcool::/resolutions/40000",
    "--outPrefix", "TADs/HiCExplorer/40000/sampleA",
    "--correctForMultipleTesting", "fdr",
]
subprocess.run(cmd, check=True)
```

```python
import cooler

print(cooler.fileops.list_coolers("hic_matrix/Corrected/sampleA.mcool"))
clr = cooler.Cooler("hic_matrix/Corrected/sampleA.mcool::/resolutions/40000")
print(clr.bins().columns)
```

```python
from pathlib import Path

domains = Path("TADs/HiCExplorer/40000/sampleA_domains.bed")
for line in domains.read_text().splitlines():
    if line.startswith("#") or not line.strip():
        continue
    chrom, start, end, tad_id = line.split()[:4]
    length = int(end) - int(start)
```

## Output Files

| File | Content |
|---|---|
| `{out-root}/{resolution}/{sample}_boundaries.bed` | TAD boundary positions. Columns from HiCExplorer: `chr`, `start`, `end`, `ID`, `separation_score`, plus optional extra BED fields. |
| `{out-root}/{resolution}/{sample}_boundaries.gff` | Boundary annotations in GFF format. |
| `{out-root}/{resolution}/{sample}_domains.bed` | TAD intervals. Common columns: `chr`, `start`, `end`, `ID`, `left_separation_score`, extra BED fields/color. |
| `{out-root}/{resolution}/{sample}_score.bedgraph` | Genome-wide separation score. Columns: `chr`, `start`, `end`, `separation_score`. |
| `{out-root}/{resolution}/{sample}_tad_score.bm` | HiCExplorer binary matrix/score output for browser or downstream HiCExplorer workflows. |
| `{out-root}/{resolution}/tad_domains_long.tsv` | Combined parsed domain table. Columns: `sample`, `resolution_bp`, `chr`, `start`, `end`, `id`, `length`, `source_file`. |
| `{out-root}/{resolution}/tad_boundaries_long.tsv` | Combined parsed boundary table. Columns: `sample`, `resolution_bp`, `chr`, `start`, `end`, `id`, `separation_score`, `source_file`. |
| `{out-root}/{resolution}/tad_domain_stats.tsv` | Per-sample domain statistics. Columns: `sample`, `resolution_bp`, `count`, `max_length`, `min_length`, `mean_length`, `median_length`, `total_length`, `n_boundaries`. |
| `{out-root}/{resolution}/tad_run_summary.tsv` | Per-sample status. Columns: `sample`, `resolution_bp`, `matrix`, `out_prefix`, `balanced`, `status`, `return_code`, `message`. |
| `{out-root}/{resolution}/tad_domain_lengths.png` | Optional domain length distribution generated when `--plot-lengths` is set. |

## Troubleshooting

| 报错信息 | 原因 | 修复命令 |
|---|---|---|
| `[ERROR] hicFindTADs not found on PATH` | 如果 healthcheck 曾成功，说明命令没有进入 `hicexplore`；否则才是 HiCExplorer 缺失或环境损坏 | 先重生成 `conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py ...`；只有 healthcheck 失败才用 `<install_tool env="hicexplore">hicexplorer</install_tool>` |
| `[ERROR] missing dependency: No module named 'cooler'` | balance/resolution preflight 需要 cooler Python 包 | `<install_tool env="hicexplore">cooler</install_tool>` |
| `[ERROR] --resolution-bp is required for .mcool` | `.mcool` 有多个分辨率，脚本不会替用户猜 | `conda run -n hicexplore python -c "import cooler; print(cooler.fileops.list_coolers('sample.mcool'))"` |
| `[ERROR] automatic balancing is disabled ...` | cooler 未 balance 且命令禁用了自动 balance，或失败类型不能通过 balance 修复 | 去掉 `--no-auto-balance`，或先提供已 balance 的矩阵 |
| `[ERROR] file does not exist: ...` | 输入路径写错，或 URI 前面的基础文件不存在 | `test -f hic_matrix/Corrected/sample.mcool && echo OK` |
| `[ERROR] sample sheet missing required column(s)` | sample sheet 没有 `sample` 和 `matrix` 列 | `head -n 1 samples.tsv` |
| `[ERROR] mixed resolutions in one run are not supported` | 多样本比较混入不同 resolution | 按 resolution 拆分 sample sheet 后分别运行 |
| `[ERROR] missing expected output: sample_domains.bed` | 输出前缀、样本名、resolution 或已有文件名不匹配 | `ls -lh TADs/HiCExplorer/40000` |
| `MemoryError`, `Killed`, or process exits without outputs | 分辨率太高、矩阵太大、线程过多导致内存不足 | `conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py --number-of-processors 4 --resolution-bp 40000 ...` |
| `[ERROR] matplotlib is required for --plot-lengths` | 可选绘图依赖缺失 | `<install_tool env="hicexplore">matplotlib</install_tool>` |

## Reference Documentation

- `references/hicfindtads_workflow.md`: Read when planning a full TAD calling workflow, deciding how to structure many samples, or explaining hicFindTADs outputs.
- `references/preflight_checks.md`: Read before running data, especially when resolution, cooler URI, or balance status is unclear.
- `references/output_schema.md`: Read before changing summary outputs, adding columns, or integrating with downstream annotation/comparison workflows.
