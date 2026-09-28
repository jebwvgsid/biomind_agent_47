---
name: differential-tad
description: 'Run HiCExplorer hicDifferentialTAD differential TAD analysis between target and control contact matrices
  using a target-specific TAD domain BED. Use when the user asks for 差异TAD, differential TADs, hicDifferentialTAD,
  target vs control TAD comparison, intra-TAD / left-inter-TAD / right-inter-TAD mode testing, rejected.diff_tad
  statistics, or bidirectional TAD comparison where both target direction and whether to run reverse comparison
  must be confirmed.

  '
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

# Differential TAD Analysis

## Overview

This skill runs `hicDifferentialTAD` for target-vs-control differential TAD testing and summarizes `accepted.diff_tad` / `rejected.diff_tad` outputs.

`--tadDomains` must correspond to the target matrix. If the target direction is wrong, the biological interpretation and output prefix are wrong.

## Mandatory Decision Before Running

Before executing data, confirm these items with the user when they are not explicit:

1. Which sample is the target and which is the control?
2. Should the analysis be one-way only, or bidirectional?
3. If bidirectional, where is the control sample's TAD domain BED?

If a prompt says to use already generated formal TAD calling results from the
same project, search formal project output locations such as
`benchmarks/biomind_agent_benchmark/runs/3d_dna/*/TADs/HiCExplorer/{resolution}/*_domains.bed`
before declaring the domains missing. Reuse a non-empty formal candidate only
when sample, resolution, and project context are unambiguous. If multiple formal
candidates match, ask the user to choose. Never use toy, fixtures, test,
expected-answer, or hidden answer-key domains as analysis input.

Formal domain discovery does not answer the biological direction question. Even
when both target and control domain BED files are found, ask the user if
target/control direction, analysis goal/background, or bidirectional mode is not
explicit in the prompt.

For bidirectional comparison, run both:

- `target_vs_control`: target matrix + control matrix + target domains
- `control_vs_target`: control matrix + target matrix + control domains

Do not infer bidirectional analysis just because two samples are provided.

## Software Environment

Use an existing environment containing HiCExplorer and this method’s documented dependencies for TAD calling, differential TAD and boundary statistics.

Verify first. If this succeeds, do not install anything:

```bash
python "${SKILL_DIR}/scripts/run_differential_tads.py" --help
```

If verification fails because the environment is missing or HiCExplorer is missing, delegate installation into the same shared environment instead of writing a conda command:

```text
先确认所需依赖（hicexplorer）与目标环境，再请求用户明确安装授权
```

If matrix resolution inspection is also needed, use the same environment for `cooler`:

```text
先确认所需依赖（cooler）与目标环境，再请求用户明确安装授权
```

After any installer delegation, re-run the verification command before running data. Never create a second TAD environment unless the user explicitly asks for a different environment name.

## Quick Start

One-way comparison matching the user's example:

```bash
python "${SKILL_DIR}/scripts/run_differential_tads.py" \
  --target leaf Matrix/Final/leaf.mcool::/resolutions/40000 \
  --control bud Matrix/Final/bud.mcool::/resolutions/40000 \
  --tad-domains TADs/HiCExplorer/40000/leaf_domains.bed \
  --out-root TADs/HiCExplorer \
  --mode all \
  --mode-reject one \
  --p-value 0.05 \
  --threads 4
```

Bidirectional comparison:

```bash
python "${SKILL_DIR}/scripts/run_differential_tads.py" \
  --target leaf Matrix/Final/leaf.mcool::/resolutions/40000 \
  --control bud Matrix/Final/bud.mcool::/resolutions/40000 \
  --tad-domains TADs/HiCExplorer/40000/leaf_domains.bed \
  --control-tad-domains TADs/HiCExplorer/40000/bud_domains.bed \
  --bidirectional \
  --out-root TADs/HiCExplorer \
  --mode all \
  --mode-reject one \
  --p-value 0.05 \
  --threads 4
```

## Outputs

For `leaf` target and `bud` control at 40000 bp resolution:

```text
TADs/HiCExplorer/40000/
├── leaf_vs_bud_accepted.diff_tad
├── leaf_vs_bud_rejected.diff_tad
├── differential_tad_run_summary.tsv
├── differential_tad_summary.tsv
└── differential_tads_rejected_long.tsv
```

If `--bidirectional` is set, the script also writes `bud_vs_leaf_accepted.diff_tad` and `bud_vs_leaf_rejected.diff_tad`.

Interpretation:

- `accepted.diff_tad`: TADs accepted by the null test.
- `rejected.diff_tad`: differential TADs at the selected p-value / reject mode.

## Parameters

| Parameter | Default | Notes |
|---|---:|---|
| `--mode` | `all` | One of `intra-TAD`, `left-inter-TAD`, `right-inter-TAD`, `all` |
| `--mode-reject` | `one` | Reject if one mode or all modes meet the test, matching `hicDifferentialTAD --modeReject` |
| `--p-value` | `0.05` | Passed to `hicDifferentialTAD --pValue` |
| `--threads` | `4` | Passed to `hicDifferentialTAD --threads` |
| `--resolution-bp` | inferred | Required for plain `.mcool` paths without `::/resolutions/N` |

## Preflight Rules

1. Verify `hicDifferentialTAD` is on `PATH` in the `hicexplore` environment.
2. Verify target and control matrix base files exist.
3. If `.mcool` paths are not full URIs, require `--resolution-bp`; do not guess.
4. Verify target and control use the same effective resolution.
5. Verify `--tad-domains` exists and belongs to the intended target direction.
6. For `--bidirectional`, verify `--control-tad-domains`; if omitted, the wrapper only infers `{out-root}/{resolution}/{control}_domains.bed` and fails if it is absent.

## Statistics

The wrapper parses `*_rejected.diff_tad` and `*_accepted.diff_tad` without requiring R.

Summary outputs:

- `differential_tad_run_summary.tsv`: command, status, input paths, output files.
- `differential_tad_summary.tsv`: per-comparison counts for accepted/rejected files and mode-specific significant counts.
- `differential_tads_rejected_long.tsv`: merged rejected differential TAD rows with comparison metadata.

Mode-specific columns count rows with p-value <= `--p-value` for:

- `intra-TAD`
- `left-inter-TAD`
- `right-inter-TAD`

## Relationship To TAD Calling

Run `skills/3d-dna/tad-calling` first to generate target-specific `_domains.bed` files. Then use this differential skill for pairwise target/control testing.
