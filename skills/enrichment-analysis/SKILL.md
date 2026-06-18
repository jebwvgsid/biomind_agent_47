---
name: enrichment-analysis
description: >
  Perform professional functional enrichment analysis for gene sets from RNA-seq
  DEG, single-cell marker genes, WGCNA modules, GWAS candidate genes, pangenome
  genes, expanded gene families, proteomics, and custom omics workflows. Use
  when Codex needs GO, KEGG/KO, Reactome/Plant Reactome, MapMan, InterPro/Pfam,
  eggNOG/COG/KOG, ORA, GSEA, compareCluster-style group enrichment, or custom
  TERM2GENE enrichment with species-aware database selection, gene ID mapping
  QC, and background-universe validation.
metadata:
  version: "1.0.0"
  legacy_ids:
    - go-kegg-enrichment
    - functional-enrichment
    - gene-set-enrichment
  tags:
    - enrichment
    - GO
    - KEGG
    - KO
    - GSEA
    - ORA
    - TERM2GENE
    - clusterProfiler
    - fgsea
    - eggNOG
    - InterPro
    - WGCNA
    - DEG
    - marker-genes
    - GWAS-candidate-genes
  task_types:
    - functional_enrichment
  inputs:
    - foreground_gene_list
    - ranked_gene_list
    - background_gene_universe
    - annotation_table
    - term2gene
  outputs:
    - input_qc
    - mapped_ids
    - enrichment_results
    - enrichment_plots
    - report
    - reproduce
  runtime:
    env_runner: Rscript or conda run -n dmr_r Rscript
    language:
      - python
      - R
    packages:
      required:
        - base R for ORA
      optional:
        - fgsea for GSEA
        - ggplot2 for enhanced plots
---

# Enrichment Analysis

## Overview

Use this skill to run statistically valid functional enrichment for biological gene sets.
The core job is to validate species, gene IDs, background universe, annotation database, mapping rate, and method choice before producing tables or plots.

## Trigger Conditions

- User provides DEG, up/down DEG, marker genes, WGCNA module genes, GWAS candidate genes, pangenome genes, expanded gene families, protein IDs, or a custom gene set.
- User asks for GO, KEGG, KO, Reactome, Plant Reactome, MapMan, Pfam, InterPro, eggNOG, COG/KOG, ORA, GSEA, pathway enrichment, functional enrichment, or over-representation analysis.
- User needs non-model species enrichment using a genome annotation table, eggNOG-mapper output, InterProScan output, KO annotation, OrthoFinder output, or a custom `TERM2GENE`.

Do not use this skill for differential expression testing itself; use an RNA-seq or single-cell DE skill first. Do not use it for plotting an already trusted enrichment table only unless the user asks for reporting or QC repair.

## Required Inputs

Identify these before running enrichment:

- Analysis type: DEG list, up/down DEG, ranked list, marker genes, module genes, candidate genes, gene families, protein list, or custom set.
- Species name and broad taxonomic group.
- Genome annotation version or database version when available.
- Gene ID type.
- Foreground genes or ranked gene list.
- Background gene universe.
- Annotation database or `TERM2GENE`.
- Output directory.

If species, gene ID type, foreground genes, appropriate background, or valid annotation source is missing, stop and request clarification or write `blocked_report.md`.

## Method Decision

Use this order:

1. If the input is a ranked gene list with statistic, log fold change, or signed score for most tested genes, prefer GSEA with `fgsea`.
2. If the input is only a significant gene list, use ORA with Fisher/hypergeometric testing.
3. If there are separate upregulated and downregulated genes, analyze up and down separately before interpreting a combined list.
4. If there are multiple clusters, modules, conditions, or gene sets, run enrichment per group and compare the result tables.
5. If the input is genomic intervals, map intervals to genes using the same genome assembly, then enrich genes.
6. If the input is proteins, map proteins to genes or use protein IDs only when the database supports those IDs.

## Database Decision

Read `references/database_decision.md` before choosing databases.

Default priority:

1. Native species annotation.
2. Curated species database such as TAIR, RAPDB, Gramene, Ensembl Plants, Phytozome, or species-specific resources.
3. Orthology-based annotation from a close species, clearly labeled as inference.
4. Custom annotation from eggNOG-mapper, InterProScan, KO, Pfam, MapMan, DIAMOND, or OrthoFinder.
5. Broad Viridiplantae/eukaryote annotation only as a last resort.

Never silently use Arabidopsis or rice annotation for a non-model plant unless orthology mapping has been performed and reported.

## Background Universe

Read `references/input_qc_and_background.md` before accepting a background.

Default background:

- RNA-seq: genes tested in differential expression after expression filtering.
- Single-cell: genes tested for marker detection or genes detected in the object.
- WGCNA: all genes used to build the network.
- GWAS/candidate regions: all genes that could have been selected under the same candidate-gene rule.
- Pangenome or gene family analysis: all gene families or genes included in the comparative analysis.
- Proteomics: all detected proteins.

Do not default to the whole genome unless the experiment genuinely tested the whole genome or the user explicitly requests it.

## Workflow

1. Confirm species, ID type, foreground, background, and annotation source.
2. Verify the execution environment. Python QC scripts use only the standard library. For the R wrapper, prefer a working existing R runner:

```bash
Rscript --version || conda run -n dmr_r Rscript --version || conda run -n rna_env Rscript --version
```

If no R runner is available, run the Python QC/build steps and write `blocked_report.md` with the missing R dependency.
3. If no `TERM2GENE` exists but an annotation table is available, build it:

```bash
python skills/enrichment-analysis/scripts/build_term2gene_from_annotation.py \
  --annotation gene_function.tsv \
  --gene-column gene_id \
  --term-columns GO,KO,PFAM,INTERPRO \
  --out-term2gene term2gene.tsv \
  --summary term2gene_summary.tsv
```

4. Run input QC before enrichment:

```bash
python skills/enrichment-analysis/scripts/check_enrichment_inputs.py \
  --foreground foreground_genes.tsv \
  --foreground-column gene_id \
  --background background_genes.tsv \
  --background-column gene_id \
  --term2gene term2gene.tsv \
  --species "Species name" \
  --gene-id-type "gene_id" \
  --analysis-type ora \
  --require-background \
  --require-term2gene \
  --outdir enrichment_out
```

5. Run ORA or GSEA:

```bash
Rscript skills/enrichment-analysis/scripts/run_enrichment.R \
  --method ora \
  --foreground foreground_genes.tsv \
  --foreground-column gene_id \
  --background background_genes.tsv \
  --background-column gene_id \
  --term2gene term2gene.tsv \
  --outdir enrichment_out \
  --species "Species name" \
  --database-name "custom TERM2GENE"
```

If bare `Rscript` is not on `PATH`, use a verified environment runner such as:

```bash
conda run -n dmr_r Rscript skills/enrichment-analysis/scripts/run_enrichment.R \
  --method ora \
  --foreground foreground_genes.tsv \
  --background background_genes.tsv \
  --term2gene term2gene.tsv \
  --outdir enrichment_out
```

For GSEA:

```bash
Rscript skills/enrichment-analysis/scripts/run_enrichment.R \
  --method gsea \
  --ranked ranked_genes.tsv \
  --ranked-gene-column gene_id \
  --rank-column stat \
  --term2gene term2gene.tsv \
  --outdir enrichment_out
```

## ID Mapping QC

Use `scripts/check_enrichment_inputs.py` before enrichment. Mapping rate refers to foreground genes found in the selected annotation/TERM2GENE universe.

| Mapping rate | Action |
|---|---|
| `< 40%` | Block the run and write `blocked_report.md`. |
| `40-70%` | Run only with a clear warning in `report.md`. |
| `> 70%` | Acceptable; still report unmapped IDs. |

Duplicated IDs must be reported and collapsed deterministically. Unmapped IDs must be written to `unmapped_ids.tsv`.

## Allowed

- Build custom `TERM2GENE` from annotation files.
- Split upregulated and downregulated genes and run enrichment separately.
- Run ORA, GSEA, or per-group enrichment when inputs support it.
- Generate TSV, plots, `report.md`, and `reproduce.sh`.
- Use orthology-based inference if clearly labeled.

## Forbidden

- Do not run enrichment if species is unknown.
- Do not use the whole genome as background by default.
- Do not mix gene IDs from different species.
- Do not treat nearest-species annotation as native annotation.
- Do not interpret raw p-values without FDR.
- Do not invent functions, pathways, terms, or p-values.
- Do not report only a plot without a result table.
- Do not merge up/down genes as the only analysis when direction is available.

## Blocked Conditions

Write `blocked_report.md` if:

- Species is unknown.
- Gene ID type cannot be determined.
- No foreground gene list or ranked gene list is available.
- No appropriate background gene universe is available for ORA.
- Mapping rate is less than 40%.
- No valid annotation database or `TERM2GENE` can be created.
- Required tools are unavailable and cannot be substituted safely.

## Required Outputs

Successful runs should include:

- `input_qc.tsv`
- `mapped_ids.tsv`
- `unmapped_ids.tsv`
- `enrichment_results.tsv`
- `enrichment_results.filtered.tsv`
- `enrichment_plots.pdf`
- `enrichment_plots.png`
- `report.md`
- `reproduce.sh`

For blocked runs:

- `blocked_report.md`
- `input_qc.tsv`, if possible

## Reporting

Read `references/reporting_standard.md` before writing `report.md`.

The report must include species, gene ID type, genome/database version, foreground count, background count, mapped count, mapping rate, database used, method, statistical test, multiple-testing correction, threshold, number of significant terms, top biological themes, limitations, and exact commands used.
