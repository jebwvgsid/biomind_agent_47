---
name: crop-gs-sommer-multikernel-gxe
description: >
  Execute professional crop genomic prediction with CRAN sommer for multi-kernel,
  GxE, dominance, family/environment random effects, multi-trait, or complex
  mixed-model designs. Use only when a model configuration is explicitly defined.
tags: [crop-breeding, genomic-selection, sommer, multi-kernel, GxE, mixed-model]
task_types: [crop_genomic_prediction]
stage_tags: [sommer_multikernel_gxe]
inputs: [genotype_matrix, phenotype_table, model_config]
outputs: [variance_components, prediction_table, model_performance, final_model]
runtime:
  default_env: crop_gp_pro
  env_runner: conda run -n crop_gp_pro Rscript
  language: R
  packages: [sommer, yaml]
  healthcheck: conda run -n crop_gp_pro Rscript -e "library(sommer); library(yaml); cat('OK\n')"
  install_command: conda install -n crop_gp_pro -c conda-forge r-sommer r-yaml -y
---

# Crop GS sommer Multi-Kernel GxE

Use this operation skill for complex GS models that cannot be represented by a
single ordinary RR-BLUP/GBLUP run. The model must be defined in
`model_config.yaml` before execution.

## Inputs

- `model_config.yaml`: declares genotype, phenotype, trait, fixed effects,
  validation design, and sommer model route.
- Real genotype and phenotype files referenced by the config.
- Optional kernel matrices or feature blocks when the design is multi-kernel.

## Professional Command

```bash
conda run -n crop_gp_pro Rscript skills/crop-breeding/crop-gs-sommer-multikernel-gxe/scripts/run_sommer_multikernel_gxe.R \
  --model-config model_config.yaml \
  --outdir results/gs_sommer
```

## Required Config Fields

- `genotype`
- `phenotype`
- `trait`
- `method`: currently `single_kernel_gblup` or a documented sommer formula route
- optional `fixed_effects`
- optional `group_column`

## Outputs

- `tables/prediction.tsv`
- `tables/ranked_breeding_materials.tsv`
- `tables/variance_components.tsv`
- `tables/model_performance.tsv`
- `model/final_model.rds`
- `reproducibility/sessionInfo.txt`
- `reproducibility/commands.sh`
- `report.md`

## Rules

- Do not guess random effects. If the config is incomplete, stop and ask for the
  biological model.
- Record the formula, kernels, fixed effects, random effects, and validation
  split.
- Use this skill for complex designs; use `crop-gs-rrblup-gblup` for ordinary
  single-kernel additive GS.
