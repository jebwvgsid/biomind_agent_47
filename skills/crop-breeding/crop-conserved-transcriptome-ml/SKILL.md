---
name: crop-conserved-transcriptome-ml
description: >
  Predict crop phenotypes from transcriptome expression using evolutionarily conserved response genes, ortholog mapping, XGBoost regression, feature importance, and optional GENIE3 regulatory-network prioritization. Use for conserved DEG-based transcriptome phenotype prediction, NUE-style gene-to-trait modeling, and candidate TF discovery; do not use for SNP-only genomic prediction.
tags: [crop-breeding, transcriptome, XGBoost, conserved-DEG, GENIE3, NUE, orthologs, 表达量预测, 候选基因]
task_types: [crop_transcriptome_prediction]
stage_tags: [conserved_transcriptome_ml]
inputs: [expression_matrix, phenotype_table, species_ortholog_table, condition_metadata]
outputs: [conserved_response_genes, xgboost_prediction, important_genes, tf_network_candidates]
when_to_use:
  - User has expression matrices plus phenotype measurements and wants interpretable gene-to-phenotype prediction.
  - User asks for conserved response genes, ortholog-guided feature reduction, XGBoost NUE prediction, or GENIE3 TF candidates.
when_not_to_use:
  - User has only genotype marker data.
  - User wants differential expression analysis only, without phenotype prediction.
legacy_ids: [conserved-transcriptome-ml, evolution-informed-ml]
runtime:
  default_env: conserved_tx_ml
  env_runner: conda run -n conserved_tx_ml
  language: mixed
  packages: [xgboost, GENIE3, GEOquery]
  capabilities: [xgboost-transcriptome-prediction, regulatory-network]
---

# Crop Conserved Transcriptome ML

Use this skill for transcriptome-based phenotype prediction with biology-guided feature reduction:

```text
expression + phenotype + condition metadata + ortholog table
-> response DEG selection
-> within- and cross-species conserved gene filtering
-> XGBoost phenotype prediction
-> feature importance
-> optional GENIE3 TF network
-> candidate gene report
```

## Source Alignment

Read `references/source-notes.md` before citing the Nature Communications NUE workflow. The original code is available through OSF, not GitHub.

## Environment And Data SOP

Read `references/install-and-data.md` before installing XGBoost/GENIE3, downloading OSF or GEO data, or claiming exact paper reproduction. Formal analysis must use XGBoost and, when TF prioritization is requested, GENIE3 or the upstream paper workflow.

## Inputs

- `expression_matrix.tsv`: rows are samples, columns are genes, values are normalized counts or expression.
- `phenotype.tsv`: `sample_id`, trait columns, genotype, species, and optional replicate columns.
- `condition_metadata.tsv`: sample condition, treatment, tissue, timepoint, genotype, and species.
- `species_ortholog_table.tsv`: ortholog or homolog mapping across species.
- `tf_list.tsv`: optional transcription factor list for GENIE3 regulator selection.

## Workflow

1. Verify that expression and phenotype come from matched or biologically comparable samples.
2. Identify treatment- or condition-responsive DEGs within each species or genotype group.
3. Intersect response genes across genotypes and then across species through the ortholog table.
4. Train XGBoost regression models with leave-genotype-out or grouped CV when genotype generalization is the goal.
5. Compare conserved-response features against random expressed genes and all expressed genes when feasible.
6. Rank important genes by XGBoost gain/cover/weight or SHAP-style attributions.
7. If TF candidates are requested, run or plan GENIE3 with response genes as targets and TF list as regulators.

## Formal Execution

Use the upstream OSF/GEO workflow or a recorded XGBoost + GENIE3 environment for exact execution. Do not substitute BioMind-local Python scripts for the paper's XGBoost/GENIE3 analysis.

## Outputs

- `conserved_response_genes.tsv`
- `xgboost_prediction.tsv`
- `model_performance.tsv`
- `important_genes.tsv`
- `tf_network_candidates.tsv`
- `candidate_gene_report.md`

## Rules

- Do not treat feature importance as proof of causality; present it as candidate prioritization.
- Keep leave-genotype-out and random-split results separate.
- Report whether features are conserved within species, across species, or both.
- Use `crop-rnaseq-de` when the task is only DEG detection; use this skill when DEG selection feeds phenotype prediction.

## Related Skills

- `crop-breeding/crop-rnaseq-de` - differential expression screening.
- `crop-breeding/crop-rnaseq-pipeline` - build expression matrices from sequencing data.
- `crop-breeding/crop-dnngp-multiomics-prediction` - broader omics and multi-omics model comparison.
- `crop-breeding/crop-smart-breeding-strategy` - choose transcriptome vs genotype prediction workflow.
