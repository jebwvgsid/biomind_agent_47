---
name: crop-smart-breeding-strategy
description: >
  Route crop breeding prediction projects to the right BioMind skill based on available data: SNP phenotype genomic prediction, GWAS-weighted Transformer, multi-omics DNNGP, GxE environmental-index prediction, pangenome/SV genomic selection, transcriptome XGBoost, phenomics, or candidate gene workflows. Use when the user asks what machine-learning phenotype prediction strategy to use or wants an integrated smart-breeding plan rather than a single model run.
tags: [crop-breeding, smart-breeding, strategy, model-selection, genomic-prediction, multi-omics, GxE, AI育种]
task_types: [crop_breeding_strategy]
stage_tags: [strategy_selection]
inputs: [data_inventory, breeding_goal, trait_metadata]
outputs: [strategy_report, selected_skills, data_requirements, validation_plan]
when_to_use:
  - User asks which phenotype prediction skill, ML model, or smart breeding route fits their data.
  - User has multiple data types and needs an execution plan.
when_not_to_use:
  - User has already selected a specific skill and asks to execute it directly.
legacy_ids: [smart-breeding-strategy, crop-breeding-router]
---

# Crop Smart Breeding Strategy

Use this as the routing and design skill for crop phenotype prediction. It does not run a model directly; it selects the right downstream BioMind skills and defines the validation plan.

## Source Alignment

Read `references/source-notes.md` when explaining the literature basis for smart breeding, integrated genomic-enviromic prediction, omics-to-breeding ML, or expanded genomic prediction.

## Decision Rules

| Available data | Recommended route |
| --- | --- |
| SNP + phenotype, standard additive GS | `crop-breeding/crop-gs-rrblup-gblup` |
| GS task needs method selection or data-readiness checks | `crop-breeding/crop-genomic-prediction` |
| SNP + phenotype, Bayesian GS requested | `crop-breeding/crop-gs-bglr-bayesian` |
| Multi-kernel, multi-trait, or GxE mixed-model GS | `crop-breeding/crop-gs-sommer-multikernel-gxe` |
| SNP + phenotype + GWAS p-values | `crop-breeding/crop-gp-waiter-transformer` |
| SNP + phenotype, no GWAS weights, deep model requested | `crop-breeding/crop-cnn-attention-genomic-prediction` |
| SNP + transcriptome/metabolome/other omics + phenotype | `crop-breeding/crop-dnngp-multiomics-prediction` |
| Multi-environment phenotype + envirotype/weather + SNP | `crop-breeding/crop-gxe-environmental-index-prediction` |
| SNP + SV/PAV/pangenome features + phenotype | `crop-breeding/crop-pangenome-sv-genomic-selection` |
| Expression + phenotype + ortholog/condition metadata | `crop-breeding/crop-conserved-transcriptome-ml` |
| Known GWAS markers/effects + new VCF samples | `crop-breeding/crop-wgs-to-marker-score` |
| Need candidate SNP-to-gene mapping | `crop-breeding/crop-gwas-pipeline` |

## Strategy Workflow

1. Inventory data: samples, traits, environments, omics blocks, variant classes, annotation, and missingness.
2. Define breeding goal: prediction accuracy, stable genotype selection, candidate gene mining, multi-trait selection, or new-environment forecasting.
3. Choose validation split before choosing models: random CV, leave-family-out, leave-year-out, leave-location-out, leave-genotype-out, or held-out target population.
4. Select a baseline-first plan. Deep learning must be compared against statistical or tree-based baselines unless the user asks for reproduction only.
5. Select interpretation output: marker effects, SHAP, attention, SNP-to-gene, haplotype, TF network, or environment sensitivity.
6. Produce a concise execution plan with selected BioMind skills and required inputs.

## Outputs

- `strategy_report.md`
- `selected_skills.tsv`
- `data_requirements.tsv`
- `validation_plan.tsv`
- `expected_outputs.tsv`

## Rules

- Do not recommend a high-impact deep model just because it is newer; match it to data type, sample size, and validation goal.
- Flag leakage risks explicitly: full-data GWAS weights, all-data normalization, phenotype-informed feature selection, and random CV across dependent environments.
- Prefer interpretable baseline models for small sample sizes and reserve deep models for sufficient sample size or explicit reproduction tasks.
- Separate prediction-for-selection from causal gene discovery.
