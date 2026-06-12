---
name: crop-pangenome-sv-genomic-selection
description: >
  Plan or run pangenome-aware crop genomic selection using SNP, indel, structural-variant, and gene presence-absence features, with heritability partitioning, GWAS, polygenic scores, and multi-trait selection indices. Use for pangenome/SV breeding datasets such as grapevine-style genomic selection; do not use for SNP-only small marker matrices unless SV or pangenome features are central.
tags: [crop-breeding, pangenome, structural-variation, SV, genomic-selection, polygenic-score, 泛基因组, 结构变异]
task_types: [crop_pangenome_selection]
stage_tags: [sv_genomic_selection]
inputs: [snp_vcf, sv_vcf, phenotype_table, gene_presence_absence, gff3]
outputs: [snp_heritability, sv_heritability, snp_vs_sv_prediction, polygenic_score, selection_index]
when_to_use:
  - User has SNP plus SV, indel, graph-pangenome, or gene presence-absence data for breeding prediction.
  - User asks for pangenome-assisted genomic selection, SV-based heritability, LDpred2, or multi-trait selection.
when_not_to_use:
  - User only has bulk RNA-seq or transcriptome expression data.
  - User needs GWAS-weighted deep learning rather than SV/pangenome feature evaluation.
legacy_ids: [crop-sv-genomic-selection, pangenome-genomic-selection, grape-pan-selection]
runtime:
  default_env: grapepan_tools
  env_runner: conda run -n grapepan_tools
  language: shell
  packages: [bcftools, vcftools, htslib, samtools, bedtools, plink, plink2]
  capabilities: [pangenome-sv-selection, variant-qc]
---

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
