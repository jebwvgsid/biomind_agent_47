---
name: differential-compartment
description: 'Compare A/B compartment or CALDER2 sub-compartment calls between samples and summarize compartment
  switches, transitions, and merged differential compartment intervals. Use when the user asks for 差异区室鉴定, differential
  compartment analysis, A-to-B or B-to-A switches, compartment transition statistics, CALDER2 all_sub_compartments
  comparison, or leaf-vs-bud compartment changes from existing compartment BED/TSV files.

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

# Differential Compartment Analysis

## Overview

Use this skill after A/B compartment calling to compare compartment labels between samples. It reads BioMind `compartments_long.tsv` or native CALDER2 `all_sub_compartments.tsv` / `.bed` files, aligns bins by `chr,start,end`, and reports transitions such as `A.1_to_B.2` or `same`.

This is a coordinate-matched transition summary, not a replicate-aware statistical test. If biological significance is required, report replicate limitations and use an appropriate replicate design.

## Quick Start

Compare two CALDER2 sample output directories:

```bash
python "${SKILL_DIR}/scripts/summarize_differential_compartments.py" \
  --sample leaf Compartments/CALDER2/40000/leaf \
  --sample bud Compartments/CALDER2/40000/bud \
  --comparison leaf bud \
  --resolution-bp 40000 \
  --out-dir Compartments/CALDER2/40000/differential_compartments
```

Compare the combined table from `compartment-calling`:

```bash
python "${SKILL_DIR}/scripts/summarize_differential_compartments.py" \
  --compartments-long Compartments/CALDER2/40000/compartments_long.tsv \
  --comparison leaf bud \
  --level Compartment4 \
  --out-dir Compartments/CALDER2/40000/differential_compartments
```

## Inputs

Accepted input forms:

- `--sample LABEL PATH`: repeat for sample-specific CALDER2 result directories or files.
- `--sample-sheet FILE`: TSV/CSV with `sample` and `compartments` or `path`.
- `--compartments-long FILE`: combined table from the compartment calling skill.

`PATH` can be:

- A sample CALDER2 output directory containing `sub_compartments/all_sub_compartments.tsv`.
- A `sub_compartments` directory.
- `all_sub_compartments.tsv` or `all_sub_compartments.bed`.
- A normalized per-sample or combined `compartments_long.tsv`.

## Outputs

For `leaf` vs `bud`, the script writes:

```text
differential_compartments/
├── leaf_vs_bud_Compartment2_transitions.tsv
├── leaf_vs_bud_Compartment4_transitions.tsv
├── leaf_vs_bud_Compartment8_transitions.tsv
├── leaf_vs_bud_differential_compartments_merged.tsv
├── compartment_transition_summary.tsv
├── compartment_pair_overlap_summary.tsv
├── differential_compartment_run_summary.tsv
└── report.md
```

Transition tables include matched bins only by default. The overlap summary reports matched and sample-specific bin counts so missing coordinates are visible.

## Rules

- Confirm all samples use the same resolution and coordinate system before comparing.
- Do not silently compare different resolutions or chromosome naming conventions.
- Use `--level Compartment2` for A/B switches and `--level Compartment4` or `--level Compartment8` for sub-compartment transitions.
- Name the direction explicitly: `leaf_vs_bud` means `leaf` labels are compared to `bud` labels and transitions are rendered as `leaf_to_bud`.
- Treat intervals with identical labels as `same`; changed intervals are rendered as `{sample1_label}_to_{sample2_label}`.
- Report that PC1/A/B sign orientation depends on the upstream compartment calling and feature-track orientation.

## Relationship To Compartment Calling

Run `skills/3d-dna/compartment-calling` first when compartment tracks do not exist. Use this skill only after per-sample compartment labels are available.
