---
name: scRNA Marker And DE Enrichment
description: 从单细胞 marker 或 DE 结果构建前景/背景/排序基因集，并交给通用 enrichment-analysis skill 执行 GO/KEGG/GSEA/TERM2GENE 富集。Use after scRNA marker discovery or DE; do not perform DE or marker testing itself.
tags: [single-cell, scRNA, enrichment, GO, KEGG, GSEA, markers, DEG, 单细胞, 富集]
task_types: [single_cell_rnaseq, functional_enrichment]
stage_tags: [scrna_enrichment, enrichment]
inputs: [marker_table, de_table, background_gene_universe, species, annotation_table]
outputs: [foreground_gene_sets, enrichment_results, enrichment_report]
when_to_use:
  - User asks for enrichment from scRNA cluster markers, cell-type markers, or single-cell DE genes.
  - Workflow stage is scrna_enrichment.
when_not_to_use:
  - User only provides a generic gene list unrelated to scRNA; use enrichment-analysis directly.
  - Species, gene IDs, foreground, or background cannot be determined.
legacy_ids: [scrna-enrichment, scrna-marker-enrichment, scrna-de-enrichment]
runtime:
  default_env: bio_scrna_core
  env_runner: python
  language: python
  packages: []
  capabilities: [scrna-enrichment-prep]
---

# scRNA Marker And DE Enrichment

Use this skill as the bridge between scRNA marker/DE tables and the general
`enrichment-analysis` skill.

## Required Inputs

- Marker table or DE table
- Species and gene ID type
- Background gene universe
- Annotation database, annotation table, or TERM2GENE
- Output directory

## Input Interpretation

- Cluster marker table: build one gene set per cluster/cell type.
- DE table: split target-up and control-up genes before enrichment.
- Ranked DE table: build a ranked list for GSEA when most tested genes are
  present with signed statistics.

## Workflow

1. Verify the marker/DE table columns.
2. Decide ORA vs GSEA from available input:
   - significant gene list -> ORA
   - ranked statistic for most tested genes -> GSEA
3. Build foreground gene sets per cluster/cell type/contrast direction.
4. Use the detected/tested genes from the Seurat object or marker/DE universe
   as the background.
5. Select and run `enrichment-analysis` with species-aware database rules.

## Related Skill

- `enrichment-analysis` - required for actual enrichment statistics and plots.

## Ask User Before Running

Ask the user when any of these are unclear:

- Species, gene ID type, or annotation source.
- Whether enrichment should be per cluster, per annotated cell type, or per
  contrast direction.
- Which table columns represent gene ID, adjusted p-value, logFC, statistic,
  cluster, and cell type.
- What background gene universe should be used.

Never use the whole genome as a default background for scRNA unless the user
explicitly requests it or the experiment genuinely tested the whole genome.

