---
name: tad-boundary-change
description: Summarize changed, shared, gained/lost, and shifted TAD boundaries across samples from existing HiCExplorer
  TAD outputs or generic boundary BED/TSV files. Use when the user asks for TAD边界改变统计, TAD边界变化, boundary gain/loss,
  boundary shift, shared/unique TAD boundaries, pairwise TAD boundary comparison, or downstream statistics after
  TAD calling without rerunning hicFindTADs.
metadata:
  version: 1.0.0
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

# TAD Boundary Change Analysis

## Overview

Use this skill after TAD calling to compare boundary positions between samples. It does not call TADs and does not run `hicDifferentialTAD`; it only parses existing boundary files and writes pairwise changed/shared/shifted boundary statistics.

The main script is:

```bash
python "${SKILL_DIR}/scripts/summarize_tad_boundary_changes.py" --help
```

## Relationship To Existing TAD Skills

- Use `skills/3d-dna/tad-calling` first when `_boundaries.bed` or `tad_boundaries_long.tsv` do not exist yet.
- Use this skill when boundary files already exist and the user wants boundary change statistics.
- Use `skills/3d-dna/differential-tad` when the user wants `hicDifferentialTAD` significance testing of TAD domains between target/control matrices.

Do not duplicate TAD calling. Do not rerun `hicFindTADs` unless the user explicitly asks to regenerate TAD outputs.

## Boundary Tolerance Policy

Default boundary matching tolerance is resolution-aware:

```text
boundary_tol_bp = boundary_tol_bins * resolution_bp
```

Default `--boundary-tol-bins` is `1`, so a 40 kb cooler uses `boundary_tol_bp=40000`, a 10 kb cooler uses `boundary_tol_bp=10000`, and so on.

Resolution may come from, in priority-checked form:

- `--resolution-bp`
- `resolution_bp` in a sample sheet or `tad_boundaries_long.tsv`
- full cooler URI such as `sample.mcool::/resolutions/40000`
- single-resolution `.cool` binsize via `cooler`
- numeric HiCExplorer output folder names such as `TADs/HiCExplorer/40000/sample_boundaries.bed`

If resolution cannot be determined, pass `--resolution-bp` or explicit `--boundary-tol-bp`. Do not guess common values such as 40000.

## Quick Start

Compare two HiCExplorer boundary BED files:

```bash
python "${SKILL_DIR}/scripts/summarize_tad_boundary_changes.py" \
  --sample leaf TADs/HiCExplorer/40000/leaf_boundaries.bed \
  --sample bud TADs/HiCExplorer/40000/bud_boundaries.bed \
  --resolution-bp 40000 \
  --out-dir TADs/HiCExplorer/40000/boundary_changes
```

Use the combined output from `tad-calling`:

```bash
python "${SKILL_DIR}/scripts/summarize_tad_boundary_changes.py" \
  --boundaries-long TADs/HiCExplorer/40000/tad_boundaries_long.tsv \
  --out-dir TADs/HiCExplorer/40000/boundary_changes
```

Infer samples from a HiCExplorer output directory:

```bash
python "${SKILL_DIR}/scripts/summarize_tad_boundary_changes.py" \
  --tad-out-dir TADs/HiCExplorer/40000 \
  --comparison leaf bud \
  --out-dir TADs/HiCExplorer/40000/boundary_changes
```

Use a manifest for portable workflows:

```bash
python "${SKILL_DIR}/scripts/summarize_tad_boundary_changes.py" \
  --sample-sheet boundary_samples.tsv \
  --comparison control treatment \
  --out-dir results/tad_boundary_changes
```

Example `boundary_samples.tsv`:

```tsv
sample	boundaries	matrix	resolution_bp
control	TADs/HiCExplorer/40000/control_boundaries.bed	Matrix/control.mcool::/resolutions/40000	40000
treatment	TADs/HiCExplorer/40000/treatment_boundaries.bed	Matrix/treatment.mcool::/resolutions/40000	40000
```

## Inputs

Accepted input forms:

- `--sample LABEL BOUNDARIES`: repeat for individual BED/TSV boundary files.
- `--sample-sheet FILE`: TSV/CSV with `sample` plus one of `boundaries`, `boundary_file`, or `path`; optional `matrix` and `resolution_bp`.
- `--boundaries-long FILE`: existing `tad_boundaries_long.tsv` from the TAD calling skill.
- `--tad-out-dir DIR`: directory containing `*_boundaries.bed`, commonly `{out-root}/{resolution}/`.

For pair selection:

- Use repeated `--comparison A B` for explicit pairwise comparisons.
- Use `--reference SAMPLE` to compare one sample against all other samples.
- If neither is provided and at least two samples exist, the script runs all pairwise comparisons.

## Outputs

The script writes:

- `tad_boundary_change_summary.tsv`: one row per comparison with shared, shifted, sample-specific, and fraction statistics.
- `tad_boundary_matches.tsv`: one row per matched boundary pair with signed and absolute shifts.
- `tad_boundary_changes_long.tsv`: long per-boundary classification table for plotting or annotation.
- `tad_boundary_sample_stats.tsv`: one row per sample with input and unique-position boundary counts.
- `tad_boundary_change_run_summary.tsv`: input files, resolution source, tolerance policy, and command metadata.
- `report.md`: concise Markdown report with input, parameter, comparison, and output summaries.
- `reproduce.sh`: executable command script for rerunning the same boundary-change analysis.
- `stage_status.tsv`: status rows for input collection, boundary comparison, and reporting.

Read `references/output_schema.md` before changing output columns.

## Environment

Prefer the shared TAD/HiCExplorer environment `hicexplore` so TAD calling, differential TAD, boundary statistics, and future Hi-C heatmap skills reuse one environment.

Reuse a successful registration covering the required capabilities without routine
health checks across stages or resume. Use its real prefix and launcher. If the
registration lacks coverage or location, inspect only that gap through configured
roots and known prefixes; ask for the location if still unresolved. A boundary-only
workflow with explicit resolution/tolerance may use its registered Python runtime.
A broader Skill workflow does not authorize extra analysis or installation.
Missing dependencies follow the task policy: stop when blocked; otherwise obtain
specific target/package/change approval through the controlled installer. Verify
new installation changes before recording success. Do not install from Skill tags.

`cooler` is optional for this script and is only needed to infer binsize from a single-resolution `.cool` file or to list resolutions from a plain `.mcool` without `::/resolutions/N`. If the user does not want to touch environments, prefer one of these:

- Add `--resolution-bp N` when comparing boundary files.
- Use full cooler URIs such as `sample.mcool::/resolutions/N`.
- Pass explicit `--boundary-tol-bp N` when only a coordinate tolerance is needed.

## Rules

- Always make the tolerance policy explicit in the command or final report.
- Prefer `--boundaries-long` or `--tad-out-dir` when consuming outputs from the existing TAD calling skill.
- Do not compare mixed resolutions unless the user explicitly passes `--allow-mixed-resolution` and an explicit `--boundary-tol-bp`.
- Treat boundary coordinates per chromosome; never match across chromosomes.
- Use boundary midpoint by default. Change `--position-mode` only if the upstream boundary file encodes a preferred point as start or end.
- Keep paths user-supplied or relative to the current project. Do not hardcode absolute paths into examples or scripts.

## Troubleshooting

| Message | Meaning | Fix |
|---|---|---|
| `resolution is required to derive --boundary-tol-bp` | No resolution source was found | Add `--resolution-bp`, use full cooler URI, or pass `--boundary-tol-bp` |
| `plain .mcool input requires explicit resolution` | `.mcool` has multiple resolutions | Use `sample.mcool::/resolutions/N` or pass `--resolution-bp N` |
| `mixed resolutions are not supported` | Samples in one comparison use different binsizes | Split by resolution or use explicit tolerance with `--allow-mixed-resolution` |
| `sample sheet missing required column` | Manifest lacks `sample` or boundary path column | Add `sample` and `boundaries` columns |
| `no boundary rows parsed` | Wrong file type or malformed columns | Check the file header and inspect `references/output_schema.md` |

## References

- `references/output_schema.md`: output columns and classification semantics.
