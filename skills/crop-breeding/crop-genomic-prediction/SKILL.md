---
name: crop-genomic-prediction-guide
description: >
  Guide crop genomic selection tasks by checking data readiness, leakage risk,
  validation design, and method fit before routing to a concrete professional GS
  skill. Use for task triage and quality boundaries; do not use as an execution
  skill or as a substitute for rrBLUP, BGLR, sommer, GEMMA, or BLUPF90.
tags: [crop-breeding, genomic-selection, method-selection, quality-gate, GS]
task_types: [crop_genomic_prediction]
stage_tags: [gs_triage]
inputs: [data_inventory, phenotype_table, genotype_data, breeding_goal]
outputs: [selected_gs_skill, data_requirements, validation_plan, leakage_warnings]
legacy_ids: [crop-genomic-prediction-guide, genomic-prediction-guide]
---

# Crop Genomic Prediction Guide

Use this skill only as the genomic selection gatekeeper. It decides whether a
real crop GS task is ready to execute and which professional downstream skill
should be loaded.

## Responsibilities

1. Identify the biological task: prediction for selection, marker-effect
   estimation, multi-environment prediction, Bayesian comparison, or causal
   candidate discovery.
2. Check required data: species/ploidy, sample IDs, phenotype definition,
   genotype format, marker coding, covariates, trial design, family/year/location
   structure, and target population.
3. Choose the validation design before any model fitting or feature selection:
   independent K-fold, held-out family, held-out location/year, held-out
   environment, or external validation population.
4. Route to one concrete operation skill:
   - `crop-breeding/crop-gs-rrblup-gblup` for standard RR-BLUP/GBLUP.
   - `crop-breeding/crop-gs-bglr-bayesian` for Bayesian whole-genome regression.
   - `crop-breeding/crop-gs-sommer-multikernel-gxe` for multi-kernel, GxE,
     dominance, multi-trait, or complex random-effect designs.
5. State interpretation boundaries: GEBV supports selection decisions only after
   validation; marker effects and GWAS associations are prioritization evidence,
   not causal proof.

## Routing Rules

| Situation | Route |
| --- | --- |
| SNP/marker matrix plus one trait, additive model, ordinary GS baseline | `crop-breeding/crop-gs-rrblup-gblup` |
| Need Bayesian shrinkage priors, posterior marker effects, or RKHS comparison | `crop-breeding/crop-gs-bglr-bayesian` |
| Need multi-kernel, GxE, dominance, family/environment random effects, or multi-trait structure | `crop-breeding/crop-gs-sommer-multikernel-gxe` |
| Need association testing or GWAS-derived weights | Run a dedicated GWAS skill first, then use fold-safe outputs in the selected prediction skill |
| Need breeding-program scenario analysis | Use a separate scenario-analysis workflow outside this GS execution skill set |

## Hard Boundaries

- Do not run analysis from this guide.
- Do not invent a model from natural-language reasoning.
- Do not use self-written sklearn/base-R substitutes for formal GS.
- Do not include local validation data in this skill.
- Do not let full-data GWAS, feature filtering, imputation, normalization, or
  scaling leak information into validation folds.
- Do not use random row-wise CV when family, environment, location, year, trial
  block, or breeding population creates dependence.

## Handoff

When routing, produce these fields for the downstream skill:

- selected skill ID
- trait column and trait direction
- genotype source and marker coding
- phenotype/covariate files
- validation split and grouping column
- required package route and citation expectation
- missing data or design blockers
