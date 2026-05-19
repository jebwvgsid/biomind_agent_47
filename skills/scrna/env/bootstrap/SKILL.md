---
name: scRNA Environment Bootstrap
description: 准备和验证单细胞转录组 Seurat 基础运行环境。用于 Seurat 对象构建、10x 数据读取、单细胞 QC 和基础可视化前的环境检查或首次安装。Use when the task is to provision or verify a reusable R environment for scRNA-seq workflows.
tags: [single-cell, scRNA, Seurat, environment, QC, 单细胞]
task_types: [single_cell_rnaseq]
stage_tags: [environment_check]
inputs: [conda_environment]
outputs: [verified_scrna_runtime]
when_to_use:
  - Workflow stage is environment_check.
  - User asks to provision or verify the reusable scRNA R environment.
when_not_to_use: [Bulk RNA-seq analysis]
legacy_ids: [scrna-env-bootstrap]
runtime:
  default_env: bio_scrna_core
  env_runner: conda run -n bio_scrna_core Rscript
  language: r
  packages: [Seurat, Matrix, yaml, ggplot2, patchwork, dplyr]
  capabilities: [scrna-env, scrna-ingest, scrna-qc]
  install_tool: '<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>'
---

# scRNA Environment Bootstrap

Use this skill to make sure the reusable Seurat runtime exists before any
single-cell analysis begins.

## Rules

- Reuse `bio_scrna_core` whenever it already satisfies the required packages.
- If the environment is missing or broken, delegate installation with:
  `<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>`
- After installation, verify with the script below instead of assuming the
  environment is ready.
- Do not create sample-specific environments.
- Plan and execution order must be:
  1. Run the verification script first.
  2. If verification fails, delegate installation to the install subagent.
  3. Re-run the verification script.
- Do not inline package-manager installation commands as the main analysis step
  for this skill.

## Quick Start

```bash
conda run -n bio_scrna_core Rscript scripts/check_scrna_env.R
```

## Fallback Install Protocol

If verification fails, emit exactly:

```text
<install_tool env="bio_scrna_core">scrna-env-bootstrap_r</install_tool>
```

Then wait for installation and registration to complete before running the
verification script again.

## Expected Output

- Package load status for core scRNA packages
- Package versions for reproducibility
- Non-zero exit if any required package fails to load

## Notes

- This skill is environment-focused. It does not ingest data or run QC itself.
- Pair it with `scrna/ingest/data-download` and `scrna/qc/filter` for the first
  course workflow.
