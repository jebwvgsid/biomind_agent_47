---
name: enrichment-analysis
description: 'Perform professional functional enrichment analysis for gene sets from RNA-seq DEG, single-cell marker
  genes, WGCNA modules, GWAS candidate genes, pangenome genes, expanded gene families, proteomics, and custom omics
  workflows. Use when Codex needs GO, KEGG/KO, Reactome/Plant Reactome, MapMan, InterPro/Pfam, eggNOG/COG/KOG, ORA,
  GSEA, compareCluster-style group enrichment, or custom TERM2GENE enrichment with species-aware database selection,
  gene ID mapping QC, and background-universe validation.

  '
metadata:
  version: 1.0.0
compatibility: 需要本技能 references/dependencies.md 列明的现有软件；不自动安装。输入输出位于分析项目。
---


## 资源定位与依赖

使用当前 Agent 的标准技能发现与文件读取能力加载本文件。`scripts/`、`references/`、
`assets/` 相对本 SKILL.md 的实际目录；输入、输出和样本清单相对本次分析项目。
命令中的 `${SKILL_DIR}` 必须先设为实际技能目录的绝对路径；该变量不由宿主自动提供。
例如，从已加载位置解析目录后，以带引号的脚本绝对路径执行，保持工作目录为分析项目，
不要在技能安装目录写结果。跨技能资源按其实际目录定位；分享完整技能包时保留目录结构。
软件和参考数据库来自已有环境或用户显式提供的位置。依赖见
[dependencies.md](references/dependencies.md)；安装、升级或修改环境需要事先明确授权。
本文命令可由任意提供命令执行能力的 Agent 或终端使用；不会在读取技能时安装或体检环境。

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
Rscript --version || Rscript --version || Rscript --version
```

If no R runner is available, run the Python QC/build steps and write `blocked_report.md` with the missing R dependency.
3. If no `TERM2GENE` exists but an annotation table is available, build it:

```bash
python "${SKILL_DIR}/scripts/build_term2gene_from_annotation.py" \
  --annotation gene_function.tsv \
  --gene-column gene_id \
  --term-columns GO,KO,PFAM,INTERPRO \
  --out-term2gene term2gene.tsv \
  --summary term2gene_summary.tsv
```

4. Run input QC before enrichment:

```bash
python "${SKILL_DIR}/scripts/check_enrichment_inputs.py" \
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
Rscript "${SKILL_DIR}/scripts/run_enrichment.R" \
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
Rscript "${SKILL_DIR}/scripts/run_enrichment.R" \
  --method ora \
  --foreground foreground_genes.tsv \
  --background background_genes.tsv \
  --term2gene term2gene.tsv \
  --outdir enrichment_out
```

For GSEA:

```bash
Rscript "${SKILL_DIR}/scripts/run_enrichment.R" \
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
