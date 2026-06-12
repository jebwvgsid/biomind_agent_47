---
name: differential-compartment-analysis
description: >
  Compare A/B compartment or CALDER2 sub-compartment calls between samples and
  summarize compartment switches, transitions, and merged differential
  compartment intervals. Use when the user asks for 差异区室鉴定, differential
  compartment analysis, A-to-B or B-to-A switches, compartment transition
  statistics, CALDER2 all_sub_compartments comparison, or leaf-vs-bud
  compartment changes from existing compartment BED/TSV files.
metadata:
  version: "1.0.0"
  tags: [compartment, differential-compartment, A/B-switch, CALDER2, sub-compartment, Hi-C, mcool, cool, 差异区室, 区室转换]
  task_types: [chromatin_contact_compartment]
  stage_tags: [differential_compartment]
  inputs: [compartment_tracks]
  outputs: [compartment_transition_tables, differential_compartment_intervals]
  when_to_use:
    - Workflow stage is differential_compartment.
    - User needs sample-to-sample compartment transition statistics.
  when_not_to_use: [compartment calling from contact matrices before tracks exist]
  legacy_ids: [diff-compartment, compartment-switch, AB-compartment-switch, 差异区室鉴定, 区室差异分析]
  runtime:
    default_env: hicexplore
    env_runner: conda run -n hicexplore python
    language: python
    packages: []
    capabilities: [differential-compartment, compartment-switch, transition-summary]
    healthcheck: conda run -n hicexplore python skills/3d-dna/differential-compartment/scripts/summarize_differential_compartments.py --help
    fallback_runner: python
---

# Differential Compartment Analysis

## Overview

Use this skill after A/B compartment calling to compare compartment labels between samples. It reads BioMind `compartments_long.tsv` or native CALDER2 `all_sub_compartments.tsv` / `.bed` files, aligns bins by `chr,start,end`, and reports transitions such as `A.1_to_B.2` or `same`.

This is a coordinate-matched transition summary, not a replicate-aware statistical test. If biological significance is required, report replicate limitations and use an appropriate replicate design.

## Quick Start

Compare two CALDER2 sample output directories:

```bash
python skills/3d-dna/differential-compartment/scripts/summarize_differential_compartments.py \
  --sample leaf Compartments/CALDER2/40000/leaf \
  --sample bud Compartments/CALDER2/40000/bud \
  --comparison leaf bud \
  --resolution-bp 40000 \
  --out-dir Compartments/CALDER2/40000/differential_compartments
```

Compare the combined table from `compartment-calling`:

```bash
python skills/3d-dna/differential-compartment/scripts/summarize_differential_compartments.py \
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
