---
name: crop-pangenome-sv-genomic-selection
description: 'Plan or run pangenome-aware crop genomic selection using SNP, indel, structural-variant, and gene
  presence-absence features, with heritability partitioning, GWAS, polygenic scores, and multi-trait selection indices.
  Use for pangenome/SV breeding datasets such as grapevine-style genomic selection; do not use for SNP-only small
  marker matrices unless SV or pangenome features are central.

  '
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

# Crop Pangenome SV Genomic Selection

Use this skill for pangenome-aware genomic breeding:

```text
SNP VCF + SV VCF + gene presence/absence + phenotype
-> QC and feature harmonization
-> GWAS and heritability partitioning
-> SNP-only vs SV-only vs combined prediction
-> polygenic score and multi-trait selection index
-> candidate SV gene report
```

## Source Alignment

Read `references/source-notes.md` before using Grapepan-specific claims. The grapevine paper provides a strong design pattern, but BioMind should generalize the workflow to other crop pangenome datasets.

## Environment And Data SOP

Read `references/install-and-data.md` before installing pangenome/SV tools, downloading GrapePan resources, or claiming exact grapevine reproduction. Formal analysis must use species-appropriate pangenome resources, VCF/PAV tooling, and a documented genomic prediction or heritability model.

## Inputs

- `snp.vcf.gz`: SNP and short indel genotypes.
- `sv.vcf.gz`: SV genotypes with coordinates and type.
- `phenotype.tsv`: `sample_id`, trait columns, optional year or replicate columns.
- `pangenome_gene_presence_absence.tsv`: optional gene family or gene PAV matrix.
- `gff3`: annotation for candidate gene mapping.
- `trait_direction.tsv`: optional high/low desired direction per trait.

## Task Scope And Shared Checks

Read `../_shared/gp-audit-and-data-validation.md` sections A–C for task evidence, matrix identity and observation checks. Apply the checks relevant to the requested analysis; complete a requested readiness audit even when a later training stage lacks inputs or resources. Keep design declarations, actual execution and stage-specific blockers separate.

## Workflow

1. Apply the shared matrix/ID checks before harmonizing SNP, SV and PAV sample labels, feature identities, allele encoding and reference coordinates. Preserve a source-position ledger and order for each feature class.
2. Identify PAV ID fields, annotation rows and metadata columns from verified schema/content; do not infer metadata-column counts from annotation-row counts. Select feature columns by verified positions or unique internal keys mapped to those positions, never by ambiguous repeated names. Reconcile raw, excluded and retained source columns. Report class-appropriate call rate, allele/presence frequency, missingness and counts; distinguish recorded 0, recorded 1 and unknown/missing values until the source establishes their biological meaning.
3. Estimate trait heritability or variance contribution by feature class when the required model/tooling is available.
4. With compatible observed training phenotypes, compare available SNP-only, SV-only, PAV-only and combined models on the same eligible samples and evaluation split; disclose coverage differences. Missing matched phenotypes block affected de novo supervised training/evaluation while format/QC continues. Preserve unlabeled targets for prediction with a valid trained model when requested.
5. Build polygenic scores or GEBV-like predictions and compare test performance by trait.
6. For multi-trait selection, standardize predicted values, apply trait directions and weights, and output a selection index.
7. Map high-impact SVs to overlapping or nearby genes and annotate candidate gene consequences.

## Formal Execution

Use the upstream GrapePan resources for grapevine reproduction or a species-specific pangenome/SV workflow for other crops. Do not substitute BioMind-local Python scripts for SNP/SV/PAV feature comparison.

## Outputs

- `snp_heritability.tsv`
- `sv_heritability.tsv`
- `pav_heritability.tsv`
- `snp_vs_sv_prediction.tsv`
- `polygenic_score.tsv`
- `multi_trait_selection_index.tsv`
- `candidate_sv_genes.tsv`
- `report.md`

## Rules

- Do not merge SNP and SV data until each class has an independent QC summary. Repeated PAV display labels do not prove duplicate features: preserve distinct source positions and values, and do not deduplicate by name. Internal technical keys are not gene/locus identifiers; ambiguous labels, invalid annotation coordinates or uncertain presence/absence semantics limit biological interpretation until a verified schema or mapping is available. Do not manufacture biological IDs or rebuild/replace the input by default.
- Report whether SVs add predictive value beyond SNPs using the same folds, not separate splits.
- Avoid human clinical risk terminology; use crop breeding terms such as predicted trait, genomic value, and selection index.
- Treat LDpred2/polygenic-score methods as optional; if LD estimation is unreliable, prefer cross-validated GEBV-style prediction.

## Related Skills

- `crop-breeding/crop-gwas-pipeline` - GWAS and SNP-to-gene mapping.
- `crop-breeding/crop-genomic-prediction` - choose the GS route and validation design.
- `crop-breeding/crop-gs-rrblup-gblup` - baseline prediction and breeding material ranking.
- `crop-breeding/crop-wgs-to-marker-score` - apply known marker effects to new VCF samples.
- `crop-breeding/crop-smart-breeding-strategy` - choose pangenome route when SV/PAV data are available.
