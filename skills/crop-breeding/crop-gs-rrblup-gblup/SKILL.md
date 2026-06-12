---
name: crop-gs-rrblup-gblup
description: >
  Execute professional crop RR-BLUP or GBLUP genomic selection with CRAN rrBLUP.
  Use for standard additive marker models, GEBV estimation, marker effects,
  cross-validated prediction accuracy, and ranking breeding materials from real
  genotype and phenotype data.
tags: [crop-breeding, genomic-selection, rrBLUP, GBLUP, GEBV]
task_types: [crop_genomic_prediction]
stage_tags: [rrblup_gblup_prediction]
inputs: [genotype_matrix, phenotype_table, trait, covariates, validation_design]
outputs: [gebv_predictions, marker_effects, model_performance, ranked_materials, final_model]
runtime:
  default_env: crop_gp_pro
  env_runner: conda run -n crop_gp_pro Rscript
  language: R
  packages: [rrBLUP]
  healthcheck: conda run -n crop_gp_pro Rscript -e "library(rrBLUP); cat('OK\n')"
  install_command: conda install -n crop_gp_pro -c conda-forge r-rrblup -y
---

# Crop GS rrBLUP/GBLUP

Use this operation skill for standard crop genomic selection with CRAN `rrBLUP`.
It is a concrete execution skill, not a guide.

## Inputs

- `genotype_matrix.tsv`: `sample_id` plus numeric marker dosage columns.
- `phenotype.tsv`: `sample_id` plus the target trait column.
- `--trait`: target trait column.
- `--method rrblup|gblup`.
- Optional `covariates.tsv` and `--covariate-cols` for fixed effects.
- Optional `--group-column` for family, year, location, or environment-held-out
  validation.

## Professional Command

```bash
conda run -n crop_gp_pro Rscript skills/crop-breeding/crop-gs-rrblup-gblup/scripts/run_rrblup_package.R \
  --genotype genotype_matrix.tsv \
  --phenotype phenotype.tsv \
  --trait trait_name \
  --method gblup \
  --folds 5 \
  --outdir results/gs_rrblup
```

Add covariates or grouped validation when required:

```bash
conda run -n crop_gp_pro Rscript skills/crop-breeding/crop-gs-rrblup-gblup/scripts/run_rrblup_package.R \
  --genotype genotype_matrix.tsv \
  --phenotype phenotype.tsv \
  --trait trait_name \
  --method rrblup \
  --covariates covariates.tsv \
  --covariate-cols PC1,PC2,year \
  --group-column family \
  --outdir results/gs_rrblup
```

## Outputs

- `tables/gebv_predictions.tsv`
- `tables/ranked_breeding_materials.tsv`
- `tables/model_performance.tsv`
- `tables/cross_validation_results.tsv`
- `tables/marker_effects.tsv` for RR-BLUP marker-effect models
- `tables/genomic_relationship_diag.tsv` for GBLUP models
- `model/final_model.rds`
- `reproducibility/sessionInfo.txt`
- `reproducibility/commands.sh`
- `report.md`

## Rules

- Use this only after the guide has fixed the validation design.
- Do not report formal results unless `rrBLUP` is installed and `sessionInfo()`
  is captured.
- Do not use random K-fold when grouped validation is biologically required.
- For polyploid crops, confirm dosage coding before running this diploid-style
  numeric marker matrix workflow.
