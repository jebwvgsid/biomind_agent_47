---
name: crop-gp-waiter-transformer
description: >
  Run or plan GP-WAITER-style crop phenotype prediction with GWAS-derived SNP weights, weighted embedding, CNN feature extraction, Transformer self-attention, SHAP/attention interpretation, and SNP-to-gene follow-up. Use when genotype markers, phenotype values, and GWAS p-values or summary statistics are available for crop genomic prediction; prefer crop-cnn-attention-genomic-prediction when no GWAS weights exist.
tags: [crop-breeding, genomic-prediction, GP-WAITER, Transformer, GWAS, SHAP, 表型预测, 基因组选择]
task_types: [crop_genomic_prediction]
stage_tags: [weighted_transformer_prediction]
inputs: [genotype_matrix, phenotype_table, gwas_summary, marker_annotation]
outputs: [prediction_table, model_performance, important_snps, attention_weights, snp_to_gene]
when_to_use:
  - User asks for GP-WAITER, GWAS-weighted Transformer, weighted SNP embedding, or interpretable crop phenotype prediction.
  - A GWAS summary table can be aligned to the genotype marker matrix.
when_not_to_use:
  - No phenotype table is available for training or validation.
  - Only transcriptome features are available without genotype markers.
legacy_ids: [gp-waiter, crop-gwas-weighted-transformer]
runtime:
  default_env: gp_waiter
  env_runner: conda run -n gp_waiter python
  language: python
  packages: [pytorch, numpy, pandas, scikit-learn]
  capabilities: [upstream-gp-waiter, gwas-weighted-prediction]
  healthcheck: conda run -n gp_waiter python -c "import torch, numpy, pandas, sklearn; print('OK')"
  install_command: mamba create -n gp_waiter -c pytorch -c nvidia -c conda-forge python=3.9 pytorch==1.13.0 pytorch-cuda=11.7 pip -y
---

# Crop GP-WAITER Transformer

Use this skill for high-priority crop phenotype prediction when GWAS-derived SNP weights are available:

```text
genotype matrix + phenotype + GWAS p-values
-> -log10(p) SNP weights
-> weighted SNP embedding
-> CNN + Transformer prediction
-> SHAP / attention interpretation
-> candidate SNP and gene report
```

## Source Alignment

Read `references/source-notes.md` before claiming paper-specific settings or reproducing the original model. The upstream implementation is an Apache-2.0 GitHub release with a compiled GP-WAITER model component; do not copy or reimplement hidden internals unless the user explicitly asks for a faithful reproduction from upstream code.

## Environment And Data SOP

Read `references/install-and-data.md` before installing GP-WAITER, checking the compiled `GP_WAITER.so`, generating GWAS weights, or discussing model downloads. For formal biological conclusions, run the upstream GP-WAITER implementation and record the exact software version.

## Inputs

- `genotype_matrix.tsv`: rows are samples, columns are SNP markers, values are normalized genotype encodings.
- `phenotype.tsv`: contains `sample_id` and one or more trait columns.
- `gwas_summary.tsv`: contains marker ID and p-value columns aligned to genotype markers.
- `marker_annotation.gff3` or SNP coordinate table: optional, for SNP-to-gene mapping.
- `covariates.tsv`: optional population structure, batch, environment, family, or year covariates.

## Workflow

1. Verify sample IDs, marker IDs, missingness, marker order, and phenotype scale.
2. Convert GWAS p-values to weights with `-log10(p)`, cap infinite values, and record the cap rule.
3. Train a professional baseline first with `crop-breeding/crop-gs-rrblup-gblup`; compare any weighted deep model against the same validation split.
4. Train GP-WAITER-style model only after the baseline split and metrics are fixed.
5. Use grouped cross-validation for family, location, year, or environment structure; never mix genetically or environmentally dependent rows across train/test folds.
6. Evaluate Pearson r, Spearman rho, MSE, RMSE, MAE, R2, training time, peak memory when available, and fold-level variance.
7. Interpret the model with attention weights and SHAP-style feature attribution; map high-priority SNPs to genes only after checking marker coordinates.

## Formal Execution

Use the upstream GP-WAITER repository for exact execution. Do not substitute BioMind-local scripts when the user expects the GP-WAITER CNN/Transformer model.

## Outputs

- `prediction.tsv`: `sample_id`, observed trait if available, predicted trait, fold, model, and split.
- `model_performance.tsv`: metrics for GP-WAITER and all baseline models.
- `important_snps.shap.tsv`: marker ID, trait, attribution score, rank, allele encoding, and direction if available.
- `attention_weight.tsv`: marker or token attention summaries with head/layer metadata when available.
- `snp_to_gene.tsv`: nearest or overlapping genes, distance, feature type, and annotation source.
- `report.md`: data QC, model comparison, interpretation, limitations, and reproducibility notes.

## Rules

- Do not use GWAS weights computed on the full dataset when reporting test performance; compute weights inside each training fold or label the result as non-independent.
- Do not report only the deep learning result. Always include statistical and machine-learning baselines unless the user explicitly asks for a pure GP-WAITER reproduction.
- Treat SHAP and attention as prioritization evidence, not causal validation.
- Warn when sample size is small relative to marker count or when GPU memory is likely to be limiting.

## Related Skills

- `crop-breeding/crop-gwas-pipeline` - generate fold-aware GWAS summaries and SNP-to-gene maps.
- `crop-breeding/crop-genomic-prediction` - choose the GS route and validation design.
- `crop-breeding/crop-gs-rrblup-gblup` - run the professional rrBLUP/GBLUP baseline.
- `crop-breeding/crop-cnn-attention-genomic-prediction` - use CNN + attention without GWAS-derived SNP weights.
- `crop-breeding/crop-smart-breeding-strategy` - choose among SNP, omics, environment, and pangenome prediction workflows.
