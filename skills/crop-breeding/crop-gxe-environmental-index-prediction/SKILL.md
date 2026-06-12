---
name: crop-gxe-environmental-index-prediction
description: >
  Plan or run CERIS-JGRA-style genotype-by-environment crop prediction by selecting environmental indices from weather or envirotype time series, fitting reaction norms, deriving genotype intercept and slope traits, and applying GWAS or genomic prediction to predict performance in new environments. Use for multi-location, multi-year, multi-environment breeding data; do not use for single-environment SNP-only prediction.
tags: [crop-breeding, GxE, envirotyping, reaction-norm, CERIS-JGRA, genomic-selection, 环境指数, 基因型环境互作]
task_types: [crop_gxe_prediction]
stage_tags: [environmental_index_prediction]
inputs: [phenotype_multi_environment, genotype_matrix, environment_timeseries, growth_stage_window]
outputs: [best_environmental_index, reaction_norm_parameters, environment_specific_prediction, stable_genotypes]
when_to_use:
  - User has phenotype observations across environments, locations, years, or management conditions.
  - User asks for environmental index, reaction norm, GxE genomic selection, CERIS, or prediction in new environments.
when_not_to_use:
  - Phenotype data come from only one environment.
  - No environmental profile or growth-window metadata can be supplied or estimated.
legacy_ids: [ceris-jgra, crop-gxe-prediction, environmental-index-prediction]
runtime:
  default_env: ceris_jgra
  env_runner: conda run -n ceris_jgra Rscript
  language: R
  packages: [data.table, tidyverse, lme4, mgcv, caret]
  capabilities: [ceris-jgra, gxe-reaction-norm]
---

# Crop GxE Environmental Index Prediction

Use this skill to reinstate the environmental dimension in crop GWAS and genomic selection:

```text
multi-environment phenotype + environment time series + genotype
-> search environmental index
-> fit reaction norm per genotype
-> derive intercept and slope traits
-> GWAS / genomic prediction
-> predict target-environment performance
```

## Source Alignment

Read `references/source-notes.md` before using CERIS-JGRA-specific terminology or exact package behavior. The upstream CERIS-JGRA code is an R repository organized by crop examples and helper functions.

## Environment And Data SOP

Read `references/install-and-data.md` before installing CERIS-JGRA, preparing environmental time series, or claiming exact R-repository reproduction. Formal analysis must use the upstream CERIS-JGRA R workflow or a recorded R implementation of the same model.

## Inputs

- `phenotype_multi_environment.tsv`: `sample_id`, `environment_id`, trait, and optional replicate/block columns.
- `genotype_matrix.tsv`: markers for GWAS or genomic prediction of reaction-norm parameters.
- `environment_timeseries.tsv`: environment variables by environment, date, and parameter.
- `growth_stage_window.tsv`: crop stage, start/end date or thermal-time window, and candidate variables.
- `target_environment.tsv`: optional new-environment profile for prediction.

## Workflow

1. Validate environment IDs, sample IDs, trait definitions, and replicate structure.
2. Define candidate environmental indices as environment parameter plus crop growth window.
3. Search for indices that explain phenotype variation and are estimable for target environments.
4. Fit a reaction norm per genotype: phenotype as intercept plus slope times environmental index.
5. Treat intercept and slope as derived traits for GWAS and genomic prediction.
6. Predict intercept and slope for unobserved genotypes, then combine them with target environmental index values.
7. Rank stable genotypes by predicted mean, slope magnitude, and trait direction.

## Formal Execution

Use the upstream CERIS-JGRA repository for exact execution. Do not substitute BioMind-local Python scripts for environmental-index search, reaction-norm fitting, or GxE genomic prediction.

## Outputs

- `best_environmental_index.tsv`
- `reaction_norm_parameters.tsv`
- `intercept_gwas.tsv`
- `slope_gwas.tsv`
- `environment_specific_prediction.tsv`
- `stable_genotypes.tsv`
- `report.md`

## Rules

- Do not assume the same environmental index works for all traits; search and report per trait.
- Check that target-environment indices are within or near the training range; extrapolation must be flagged.
- Keep intercept and slope interpretation separate: intercept is baseline performance, slope is environmental sensitivity.
- Use environment- or year-held-out validation to test new-environment prediction, not only random row-wise CV.

## Related Skills

- `crop-breeding/crop-gwas-pipeline` - GWAS for derived intercept and slope traits.
- `crop-breeding/crop-genomic-prediction` - choose the GS route and validation design.
- `crop-breeding/crop-gs-rrblup-gblup` - baseline genomic prediction for intercept/slope traits.
- `crop-breeding/crop-gp-waiter-transformer` - weighted Transformer prediction if fold-specific GWAS weights are available.
- `crop-breeding/crop-smart-breeding-strategy` - decide whether a GxE route is warranted.
