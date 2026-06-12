---
name: crop-cnn-attention-genomic-prediction
description: >
  Train, plan, or interpret Cropformer-style crop genomic prediction using genotype encodings, CNN layers, multi-head self-attention, attention weights, SHAP, and haplotype follow-up. Use as the unweighted attention genomic prediction route when genotype and phenotype are available but GWAS-derived SNP weights are absent; use the weighted Transformer skill when fold-safe GWAS weights are central.
tags: [crop-breeding, Cropformer, CNN, self-attention, genomic-prediction, SHAP, haplotype, 表型预测]
task_types: [crop_genomic_prediction]
stage_tags: [cnn_attention_prediction]
inputs: [genotype_matrix, phenotype_table, marker_annotation]
outputs: [predicted_trait, important_snp, attention_weights, haplotype_candidates]
when_to_use:
  - User asks for Cropformer, CNN-attention genomic prediction, attention-based SNP mining, or unweighted Transformer phenotype prediction.
  - Genotype and phenotype are available but GWAS weights are missing or should not be used.
when_not_to_use:
  - User has GWAS p-value weights and wants the weighted Transformer route specifically.
  - User has only expression/omics matrices without genotype markers.
legacy_ids: [cropformer, crop-cnn-attention]
runtime:
  default_env: cropformer
  env_runner: conda run -n cropformer python
  language: python
  packages: [pytorch, numpy, pandas]
  capabilities: [upstream-cropformer, attention-genomic-prediction]
---

# Crop CNN Attention Genomic Prediction

Use this skill for Cropformer-style genomic prediction:

```text
genotype matrix + phenotype
-> genotype encoding
-> CNN local feature extraction
-> multi-head self-attention
-> phenotype prediction
-> attention / SHAP / haplotype interpretation
```

## Source Alignment

Read `references/source-notes.md` before using Cropformer-specific defaults. The upstream repository is PyTorch-based and expects users to edit paths in scripts.

## Environment And Data SOP

Read `references/install-and-data.md` before installing Cropformer, preparing upstream CSV inputs, or discussing model downloads. For formal biological conclusions, run the upstream Cropformer implementation and record the exact software version.

## Inputs

- `genotype_matrix.tsv`: marker matrix or preprocessed genotype CSV.
- `phenotype.tsv`: `sample_id`, trait columns, and optional environment/family columns.
- `marker_annotation.tsv` or `gff3`: optional marker coordinates and gene mapping.
- `haplotype_region.tsv`: optional regions for haplotype analysis after SNP prioritization.

## Workflow

1. Validate marker encoding and sample matching.
2. Train the professional rrBLUP/GBLUP baseline with `crop-breeding/crop-gs-rrblup-gblup` first.
3. If using a Cropformer-like route, choose marker filtering or feature selection inside the training fold.
4. Encode genotypes consistently; preserve marker position whenever interpretation is needed.
5. Train CNN + multi-head self-attention with early stopping and fixed folds.
6. Extract attention weights and run SHAP-style or permutation attribution where feasible.
7. Map prioritized SNPs to genes and optionally summarize haplotypes in candidate regions.

## Formal Execution

Use the upstream Cropformer repository for exact execution. Do not substitute BioMind-local scripts when the user expects the original PyTorch CNN/self-attention model.

## Outputs

- `predicted_trait.tsv`
- `model_performance.tsv`
- `important_snp.tsv`
- `attention_weight.tsv`
- `candidate_gene.tsv`
- `haplotype_candidates.tsv`
- `report.md`

## Rules

- Do not call attention weights causal evidence.
- If feature selection uses phenotype information, run it inside each training fold.
- Do not replace `crop-gp-waiter-transformer` when validated GWAS weights are available and the user asks for GP-WAITER.
- Warn about deep-learning instability for small sample sizes.

## Related Skills

- `crop-breeding/crop-genomic-prediction` - choose the GS route and validation design.
- `crop-breeding/crop-gs-rrblup-gblup` - professional rrBLUP/GBLUP baseline.
- `crop-breeding/crop-gp-waiter-transformer` - GWAS-weighted Transformer model.
- `crop-breeding/crop-gwas-pipeline` - GWAS and SNP-to-gene mapping.
- `crop-breeding/crop-smart-breeding-strategy` - choose appropriate prediction skill.
