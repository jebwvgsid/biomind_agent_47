---
name: scRNA Analysis Summary Report
description: 汇总 scRNA 分析全过程的 QC、预处理、聚类、注释、marker、DE、富集和轨迹结果，生成报告、stage_status.tsv 和 reproduce.sh。Use at the end of a scRNA workflow; do not use as a substitute for missing analysis steps.
tags: [single-cell, scRNA, report, summary, reproduce, 单细胞, 报告]
task_types: [single_cell_rnaseq]
stage_tags: [scrna_report]
inputs: [analysis_outputs, stage_status, reproduce_script]
outputs: [report, stage_status, reproduce_script]
when_to_use:
  - Workflow stage is scrna_report.
  - User asks whether a single-cell analysis finished correctly.
when_not_to_use:
  - Required upstream outputs are missing and no analysis has been run.
legacy_ids: [scrna-summary-report, scrna-report]
runtime:
  default_env: bio_scrna_core
  env_runner: bash
  language: bash
  packages: []
  capabilities: [scrna-report]
---

# scRNA Analysis Summary Report

Use this skill to create a final, auditable summary after scRNA analysis.

## Required Inputs

- Output directory containing one or more standard BioMind scRNA outputs
- The commands or scripts used for reproduction
- Stage completion evidence

## Workflow

1. Verify expected artifacts for the selected workflow stages.
2. Summarize cell counts, filtering, clustering, annotation, marker, DE,
   enrichment, and trajectory outputs that actually exist.
3. Record missing optional outputs as `not_run` or `not_requested`, not failed.
4. Write a concise scientific report with methods, parameters, and limitations.
5. Write `stage_status.tsv` and `reproduce.sh`.

## Outputs

- `{output_dir}/report.md`
- `{output_dir}/stage_status.tsv`
- `{output_dir}/reproduce.sh`

## Rules

- Do not mark the workflow complete if the latest executed command only wrote a
  script and did not run the promised analysis.
- Do not invent results for optional stages that were not requested.
- If an upstream required artifact is missing, ask the user or report the exact
  missing artifact instead of looping through read-only checks.

