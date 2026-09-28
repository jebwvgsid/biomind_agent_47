---
name: tad-calling
description: 'Run generalized TAD analysis / TAD calling with HiCExplorer hicFindTADs for one or many Hi-C, Micro-C,
  Pore-C, or chromatin contact matrix samples. Use when the user asks for TAD分析, TAD calling, hicFindTADs, HiCExplorer,
  拓扑关联结构域, TAD边界, TAD domains, separation score, domain length statistics, multi-sample TAD batch analysis, sample
  sheet workflows, resolution confirmation, or balance checks for .cool/.mcool matrices.

  '
metadata:
  version: 1.2.1
compatibility: 需要本技能 references/dependencies.md 列明的现有软件；不自动安装。输入输出位于分析项目。
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

# TAD Analysis with hicFindTADs

## Overview

This skill runs HiCExplorer `hicFindTADs` on one or many `.cool/.mcool` contact matrices, after confirming the resolution and ensuring that the cooler has usable balance weights.
It writes native HiCExplorer outputs plus combined multi-sample tables for domains, boundaries, and domain length statistics.

Resolution is a required analysis setting, not a separate file input role.
For example, a confirmed 40kb resolution means the integer setting
`resolution_bp=40000`, passed as `--resolution-bp 40000`; do not declare an
input path `"40000"` to represent that setting. Record the selected setting and its source in the analysis notes.
User values cite their actual turn; Skill defaults cite this Skill/version and
remain defaults even if adopted. Record the selected value as a method parameter.
If the required resolution is unknown, retain that uncertainty before execution.
Keep user-declared matrix paths as inputs even before inspection, with their
unverified status explained; do not invent availability. Explicit matrix URIs
such as `sample.mcool::/resolutions/40000` remain resource references whose
internal group also specifies the resolution, as described below.

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
python "${SKILL_DIR}/scripts/call_tads.py" \
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

Use an existing environment containing HiCExplorer and this method’s documented dependencies for TAD calling, differential TAD and boundary statistics. Do not emit direct package-manager installation commands as an execution step.

When an existing successful registration covers the needed tools and capabilities,
use its actual prefix and launcher directly. Do not repeat `--help`, version,
import or health checks merely because the conversation or cache changed.
Registration time is history, not an expiry. The host’s launch environment need not be the scientific environment.
If registration is incomplete, locate only the missing part through configured
Conda roots and known prefixes. Ask for an environment location if unresolved.
A failed input read or timeout does not establish a missing dependency.
Honor the task's dependency policy: blocked means stop and explain; an installation
requires a concrete target, missing packages, change scope and explicit user
approval through the existing installer. Verify the new change before registration.
Validate changed dependencies only when needed; reading this method performs no checks.

## Agent Decision Protocol

### Lightweight notes and explicit scientific checks

Keep optional notes of goals, method, parameters and a few milestones.
Reference the original sample sheet once with its actual columns and relative
path base. Keep the sample sheet as the canonical list rather than duplicating every row. Batch scripts consume all rows and return summary plus detail paths.

Call the resolution and balance checks below explicitly and read their actual
results. A model conclusion cannot rewrite a negative tool result. Report any
unperformed or failed check. Continue in the current analysis workflow, preserving the actual observations.
Controlled script drafts still use complete reads and CAS patches; saving or
syntax checking is not execution. End with the actual full/partial outcome.

### Resolution-only validation

For a step that only checks input resolution availability, use the registered
`scripts/check_cooler_resolution.py` resource at its exact registered Skill path.
Use the adopted registered runtime prefix/interpreter (not an assumed environment
name), one unbuffered Python process for all inputs, with repeated
`--input <declared-sample> <exact-approved-input-path>` and
`--resolution-bp <approved-positive-integer>`. The script returns plain JSON with
exact input paths for the model to assess. No generated binder variable or
observation handle is required; omit --observation. Existing legacy
callers may still pass their actual declared --observation handle.
The same query result supplies display, exact group comparison and recorded evidence. Do not rewrite this query as inline Python,
compare integers to group paths, or query again for the decision. A negative
result exits nonzero while preserving the negative observation. Query errors do
not mean the resolution is absent. Metadata presence does not validate the full
matrix or its balance. This entry neither balances nor writes inputs.

### Mandatory preflight — confirm resolution and balance

Before running `hicFindTADs`, do these checks:

1. Reuse a covering registration directly; resolve only a real registration gap under the task dependency policy.
2. If the user provides plain `.mcool` paths and no explicit resolution, ask the user which resolution to use. Do not guess from common values like 40000.
3. If paths are full URIs such as `sample.mcool::/resolutions/40000`, treat that as explicit resolution confirmation.
4. Prefer one confirmed resolution per multi-sample comparison. Do not combine different resolutions in one summary unless the user explicitly asks for separate runs.
5. Check the `weight` column with `--preflight-only` (read-only). Missing weights are an input condition, not missing dependencies. Automatic balance is disabled by default: `--auto-balance` writes the effective matrix URI. The plan must name the original input or working copy, exact target and approved write scope. Prefer a working copy under the existing approval boundary; never treat selecting `weight` as permission to modify an input.
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
python "${SKILL_DIR}/scripts/check_mcool_balance.py" \
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
python "${SKILL_DIR}/scripts/call_tads.py" --sample leaf /path/to/leaf.mcool --sample bud /path/to/bud.mcool --resolution-bp 40000 --out-root TADs/HiCExplorer --preflight-only
```

This command writes `{out-root}/{resolution_bp}/tad_preflight.tsv`, with columns
`sample`, `resolution_bp`, `matrix`, `balanced`, `status`, and `message`.
Declare that exact output with `role: "tad_preflight"`: the host checks every
declared sample and input URI, the required resolution, `balanced=yes`, and
`status=success`. The dedicated `check_mcool_balance.py` output described above
has a different schema; it is not an output of `call_tads.py --preflight-only`.
Reports, reproduction scripts and stage-status tables are separate delivery work.

Preflight never overwrites `tad_preflight.tsv`: if that exact path already exists,
select an explicitly authorized new output location or use the existing receipt
through the host's evidence review. No balance check is rerun on this conflict.
Unrelated BED/cache files do not block writing a new preflight table. Conversely,
an existing preflight table alone does not block a later TAD execution in the same
resolution directory.

For TAD calling, declare the actual `tad_run_summary.tsv` with
`role: "tad_run_summary"` and domains/boundaries BED outputs with `format: "bed"`.
These existing checks consume the saved files; they do not require a fabricated
observation JSON or a repeated scientific run. Keep the scientific interpretation
and report-consistency requirements in the description and evidence-based
`assessment`. Neither a readable table nor the outer process exit code alone
establishes that every sample succeeded.

Resolution discovery command when the user has not specified a resolution:

```bash
python -c "import cooler; print(cooler.fileops.list_coolers('/path/to/sample.mcool'))"
```

Do not use bare commands such as `cooler info sample.mcool` unless `which cooler` has been checked and does not point to a stale `bin_shims/cooler`. Prefer `cooler ...` or `python -c "import cooler; ..."` for HiCExplorer workflows.

### Tier 1 — 直接调用脚本

**触发条件**：标准多样本或单样本 hicFindTADs TAD 分析；用户已经确认样本路径、输出目录、分辨率，并允许默认 balance handling。

For many samples, use a sample sheet:

```bash
python "${SKILL_DIR}/scripts/call_tads.py" \
  --sample-sheet /path/to/samples.tsv \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer \
  --correct-for-multiple-testing fdr
```

For a small number of samples, repeat `--sample`:

```bash
python "${SKILL_DIR}/scripts/call_tads.py" \
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
| "只统计已有结果，不重新跑" | `--stats-only`，新汇总位置用 `--summary-out-dir` |
| "结果已存在就跳过运行" | `--skip-existing`，新汇总位置用 `--summary-out-dir` |
| "重新运行已有分析" | 明确选择新的 `--out-root`；`--force` 只取消 skip 选择，不授权覆盖或清理缓存 |
| "不要自动 balance，缺 weight 就报错" | `--no-auto-balance` |
| "多重检验用 fdr" | `--correct-for-multiple-testing fdr` |
| "限制染色体 Chr03" | `--chromosome Chr03` |
| "这个矩阵没有 weight 但我确认已校正" | `--skip-balance-check` |
| "加 hicFindTADs 原生命令参数" | `--extra-arg=--minDepth --extra-arg=80000` |
| "画 domains 长度分布" | `--plot-lengths` |

Do NOT touch the script for these changes.

Before execution the wrapper checks this batch's native output prefixes, logs,
z-score/TAD-score caches, and intended summary files. An occupied target is an
`OUTPUT_CONFLICT`; old bytes remain untouched. It does not delete caches or pass
an invented force option to HiCExplorer. In particular, a leftover
`*_tad_score.bm` is never implicitly reused, even if its z-score partner is missing.
Use a newly declared run location or the host's existing explicit conflict and
authorization flow. `--extra-arg` cannot override matrix/output bindings or enable
`--TAD_sep_score_prefix`; scientific tuning flags remain available.

`--stats-only` rows are `read_existing`; `--skip-existing` rows that read old native
results are `reused`. Their per-sample `return_code` is empty: outer wrapper exit 0
only means the requested reading/statistics operation completed. These rows are
not new scientific successes and cannot satisfy a new-execution `tad_run_summary`
or `sample_results` check. Keep the original native `source_file` paths and review
original task/attempt, method, input versions, coverage and contents through the
existing evidence path. Cross-session automatic acceptance is not implied, and a
historical prefix fingerprint cannot be upgraded to full-file evidence.

`--summary-out-dir` is available only with `--stats-only` or `--skip-existing` and
names the direct destination for new combined tables/optional plot, without
changing the native `--out-root/RESOLUTION` paths. Record its real paths in the
analysis notes. Existing summary files are refused there too. Failures, skipped/unattempted
samples, reused samples and read-only statistics are counted separately; TSV
fields use CSV quoting so multiline/tab/quote-bearing errors remain intact.

### Tier 3 — 读脚本并修改

**触发条件**：用户需要新增统计列、改变输出 schema、合并基因注释、比较两个样本 TAD 重叠、改图形类型，或支持非 HiCExplorer 格式。

触发关键词：

- 中文：`新增统计`, `合并注释`, `比较重叠`, `改输出列`, `改图`, `换成箱线图`, `只保留某类TAD`, `计算差异TAD`
- English: `custom stats`, `merge annotation`, `overlap TADs`, `change columns`, `boxplot`, `differential TAD`, `custom parser`

操作步骤（Linux/POSIX shell）：

```bash
# 1. Read the script first
cat "${SKILL_DIR}/scripts/call_tads.py"

# 2. Copy to a working file. NEVER edit files inside the skill directory.
cp "${SKILL_DIR}/scripts/call_tads.py" ./call_tads_custom.py

# 3. Modify only the copied script, then run it.
python ./call_tads_custom.py --sample-sheet samples.tsv --resolution-bp 40000 --out-root TADs/HiCExplorer
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
python "${SKILL_DIR}/scripts/call_tads.py" \
  --sample-sheet samples.tsv \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer \
  --correct-for-multiple-testing fdr
```

Small batch using repeated `--sample`:

```bash
python "${SKILL_DIR}/scripts/call_tads.py" \
  --sample leaf hic_matrix/Corrected/leaf.mcool \
  --sample bud hic_matrix/Corrected/bud.mcool \
  --sample root hic_matrix/Corrected/root.mcool \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer
```

Full URI input where resolution is encoded in every matrix path:

```bash
python "${SKILL_DIR}/scripts/call_tads.py" \
  --sample sampleA hic_matrix/Corrected/sampleA.mcool::/resolutions/40000 \
  --sample sampleB hic_matrix/Corrected/sampleB.mcool::/resolutions/40000 \
  --out-root TADs/HiCExplorer
```

Generate only statistics from existing HiCExplorer outputs:

```bash
python "${SKILL_DIR}/scripts/call_tads.py" \
  --sample-sheet samples.tsv \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer \
  --stats-only \
  --summary-out-dir TADs/review_summary_NEW
```

Run when balance has been externally verified but no `weight` column exists:

```bash
python "${SKILL_DIR}/scripts/call_tads.py" \
  --sample-sheet samples.tsv \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer \
  --skip-balance-check
```

Pass advanced hicFindTADs options without editing the script:

```bash
python "${SKILL_DIR}/scripts/call_tads.py" \
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
| `[ERROR] hicFindTADs not found on PATH` | 如果 healthcheck 曾成功，说明命令没有进入 `hicexplore`；否则才是 HiCExplorer 缺失或环境损坏 | 先重生成 `python "${SKILL_DIR}/scripts/call_tads.py" ...`；只有 healthcheck 失败才用 `先确认所需依赖（hicexplorer）与目标环境，再请求用户明确安装授权` |
| `[ERROR] missing dependency: No module named 'cooler'` | balance/resolution preflight 需要 cooler Python 包 | `先确认所需依赖（cooler）与目标环境，再请求用户明确安装授权` |
| `[ERROR] --resolution-bp is required for .mcool` | `.mcool` 有多个分辨率，脚本不会替用户猜 | `python -c "import cooler; print(cooler.fileops.list_coolers('sample.mcool'))"` |
| `[ERROR] automatic balancing is disabled ...` | cooler 未 balance 且命令禁用了自动 balance，或失败类型不能通过 balance 修复 | 去掉 `--no-auto-balance`，或先提供已 balance 的矩阵 |
| `[ERROR] file does not exist: ...` | 输入路径写错，或 URI 前面的基础文件不存在 | `test -f hic_matrix/Corrected/sample.mcool && echo OK` |
| `[ERROR] sample sheet missing required column(s)` | sample sheet 没有 `sample` 和 `matrix` 列 | `head -n 1 samples.tsv` |
| `[ERROR] mixed resolutions in one run are not supported` | 多样本比较混入不同 resolution | 按 resolution 拆分 sample sheet 后分别运行 |
| `[ERROR] missing expected output: sample_domains.bed` | 输出前缀、样本名、resolution 或已有文件名不匹配 | `ls -lh TADs/HiCExplorer/40000` |
| `MemoryError`, `Killed`, or process exits without outputs | 分辨率太高、矩阵太大、线程过多导致内存不足 | `python "${SKILL_DIR}/scripts/call_tads.py" --number-of-processors 4 --resolution-bp 40000 ...` |
| `[ERROR] matplotlib is required for --plot-lengths` | 可选绘图依赖缺失 | `先确认所需依赖（matplotlib）与目标环境，再请求用户明确安装授权` |

## Reference Documentation

- `references/hicfindtads_workflow.md`: Read when planning a full TAD calling workflow, deciding how to structure many samples, or explaining hicFindTADs outputs.
- `references/preflight_checks.md`: Read before running data, especially when resolution, cooler URI, or balance status is unclear.
- `references/output_schema.md`: Read before changing summary outputs, adding columns, or integrating with downstream annotation/comparison workflows.
