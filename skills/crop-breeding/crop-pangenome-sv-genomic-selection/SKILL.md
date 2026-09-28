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

## Workflow

1. Harmonize sample IDs and allele encoding across SNP, SV, and PAV matrices.
2. Run QC separately for SNPs, indels, SVs, and PAVs; report call rate, MAF, missingness, and variant class counts.
3. Estimate trait heritability or variance contribution by feature class when the required model/tooling is available.
4. Train SNP-only, SV-only, PAV-only, and combined genomic prediction models using the same train/tune/test split.
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

- Do not merge SNP and SV data until each class has independent QC summaries.
- Report whether SVs add predictive value beyond SNPs using the same folds, not separate splits.
- Avoid human clinical risk terminology; use crop breeding terms such as predicted trait, genomic value, and selection index.
- Treat LDpred2/polygenic-score methods as optional; if LD estimation is unreliable, prefer cross-validated GEBV-style prediction.

## Related Skills

- `crop-breeding/crop-gwas-pipeline` - GWAS and SNP-to-gene mapping.
- `crop-breeding/crop-genomic-prediction` - choose the GS route and validation design.
- `crop-breeding/crop-gs-rrblup-gblup` - baseline prediction and breeding material ranking.
- `crop-breeding/crop-wgs-to-marker-score` - apply known marker effects to new VCF samples.
- `crop-breeding/crop-smart-breeding-strategy` - choose pangenome route when SV/PAV data are available.
