---
name: crop-gs-bglr-bayesian
description: >
  Execute Bayesian crop genomic selection with CRAN BGLR. Use for BRR, BayesA,
  BayesB, BayesC, Bayesian LASSO, and RKHS models when posterior shrinkage,
  marker-effect uncertainty, or Bayesian model comparison is required.
tags: [crop-breeding, genomic-selection, BGLR, Bayesian, RKHS]
task_types: [crop_genomic_prediction]
stage_tags: [bglr_bayesian_prediction]
inputs: [genotype_matrix, phenotype_table, trait, covariates, bayesian_model, mcmc_settings]
outputs: [prediction_table, model_performance, posterior_summary, final_model]
runtime:
  default_env: crop_gp_pro
  env_runner: conda run -n crop_gp_pro Rscript
  language: R
  packages: [BGLR]
---

# Crop GS BGLR Bayesian

Use this operation skill when the selected GS method is Bayesian whole-genome
regression through CRAN `BGLR`.

## Inputs

- `genotype_matrix.tsv`: `sample_id` plus numeric marker dosage columns.
- `phenotype.tsv`: `sample_id` plus target trait.
- `--model BRR|BayesA|BayesB|BayesC|BL|RKHS`.
- `--niter`, `--burnin`, `--thin`, and `--seed`.
- Optional covariates and grouped validation.

## Professional Command

```bash
conda run -n crop_gp_pro Rscript skills/crop-breeding/crop-gs-bglr-bayesian/scripts/run_bglr_bayesian.R \
  --genotype genotype_matrix.tsv \
  --phenotype phenotype.tsv \
  --trait trait_name \
  --model BRR \
  --niter 12000 \
  --burnin 2000 \
  --thin 5 \
  --outdir results/gs_bglr
```

## Outputs

- `tables/prediction.tsv`
- `tables/ranked_breeding_materials.tsv`
- `tables/model_performance.tsv`
- `tables/cross_validation_results.tsv`
- `tables/posterior_marker_effects.tsv` when the selected BGLR model exposes marker effects
- `model/final_model.rds`
- `reproducibility/sessionInfo.txt`
- `reproducibility/commands.sh`
- `report.md`

## Rules

- State the prior/model (`BRR`, `BayesA`, `BayesB`, `BayesC`, `BL`, or `RKHS`)
  in every report.
- Record MCMC settings and seed.
- Treat posterior marker effects as model-based prioritization evidence, not
  causal proof.
- Do not compare Bayesian and non-Bayesian models unless they share the same
  validation split.
