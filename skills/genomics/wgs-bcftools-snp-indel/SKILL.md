---
name: wgs-bcftools-snp-indel
description: Call short-read WGS SNPs and InDels with bcftools mpileup and call, then filter, split SNP/InDel VCFs, index, and summarize. Use for fast lightweight resequencing variant calling on small or medium cohorts when GATK GVCF joint genotyping is unnecessary or unavailable. Do not present it as equivalent to the GATK workflow for high-confidence large cohorts.
version: "1.0.0"
tags: [WGS, bcftools, mpileup, SNP, InDel, lightweight, variant-calling]
legacy_ids: [bcftools-snp-indel, wgs-bcftools, samtools-bcftools-variant]
task_types: [wgs_snp_indel_calling]
stage_tags: [bcftools_mpileup, variant_filtering]
inputs: [reference_fasta, sorted_bam, bam_list]
outputs: [raw_vcf, filtered_vcf, filtered_snp_vcf, filtered_indel_vcf, stats]
runtime:
  default_env: bio_bcftools
  env_runner: bash
  language: bash
  packages: [bcftools, samtools, htslib]
  capabilities: [mpileup, variant-calling, vcf-filtering]
  healthcheck: bash -lc "bcftools --version >/dev/null && samtools --version >/dev/null"
---

# WGS bcftools SNP/InDel Calling

Use this skill for a fast short-read WGS SNP/InDel workflow. It is practical for small/medium cohorts, quick checks, and clusters without a full GATK setup.

## 确认任务边界

Use this skill when:

- Input BAMs are WGS, coordinate-sorted, indexed, and aligned to one reference.
- The user wants a lightweight caller or exploratory SNP/InDel set.
- Cohort size and compute constraints make GATK GVCF unnecessary.

Do not use this skill when:

- The user requested GATK best-practice GVCF joint genotyping.
- Input is RNA-seq; use RNA-seq variant calling.
- The target is SV or CNV.

State clearly in reports that bcftools and GATK can produce different callsets.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `REFERENCE_FASTA` | FASTA with `.fai` |
| `BAM_LIST` | one BAM path per line or `sample<TAB>bam` normalized before use |
| `OUTDIR` | output directory |

Recommended:

| Parameter | Default / notes |
|---|---|
| `THREADS` | 4-16 |
| `MIN_QUAL` | 10 for permissive first pass, tune by project |
| `REGIONS` | optional BED or region list |
| `CALL_MODE` | `--multiallelic-caller --variants-only` |

Preflight:

```bash
bcftools --version
test -s "${REFERENCE_FASTA}.fai" || samtools faidx "${REFERENCE_FASTA}"
samtools quickcheck -v $(cat "${BAM_LIST}")
```

Core call:

```bash
bcftools mpileup \
  --bam-list "${BAM_LIST}" \
  --threads "${THREADS:-8}" \
  --output-type u \
  --fasta-ref "${REFERENCE_FASTA}" |
bcftools call \
  --threads "${THREADS:-8}" \
  --multiallelic-caller \
  --variants-only \
  --output-type z \
  --output "${OUTDIR}/03.variant_calling/all.raw.vcf.gz"
```

Basic filter and split:

```bash
bcftools filter -g 3 -G 10 -e "%QUAL<${MIN_QUAL:-10}" \
  --output-type z \
  --output "${OUTDIR}/04.filter/all.filtered.vcf.gz" \
  "${OUTDIR}/03.variant_calling/all.raw.vcf.gz"

bcftools view --types snps --output-type z \
  --output "${OUTDIR}/04.filter/all.filtered.snp.vcf.gz" \
  "${OUTDIR}/04.filter/all.filtered.vcf.gz"

bcftools view --types indels --output-type z \
  --output "${OUTDIR}/04.filter/all.filtered.indel.vcf.gz" \
  "${OUTDIR}/04.filter/all.filtered.vcf.gz"
```

## 输出结果

Expected outputs:

- `03.variant_calling/all.raw.vcf.gz`
- `04.filter/all.filtered.vcf.gz`
- `04.filter/all.filtered.snp.vcf.gz`
- `04.filter/all.filtered.indel.vcf.gz`
- `.tbi` indexes for all compressed VCFs.
- `05.summary/*.stats.txt`
- `05.summary/variant_counts.tsv`

Validation:

```bash
for vcf in "${OUTDIR}"/03.variant_calling/all.raw.vcf.gz "${OUTDIR}"/04.filter/*.vcf.gz; do
  bcftools index -t "$vcf"
  bcftools stats "$vcf" > "${OUTDIR}/05.summary/$(basename "$vcf").stats.txt"
done
bcftools view -h "${OUTDIR}/04.filter/all.filtered.snp.vcf.gz" >/dev/null
```

Report sample count, raw count, filtered SNP count, filtered InDel count, and filter expression.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| `mpileup` contig mismatch | BAM and FASTA references differ | use matching FASTA or re-align |
| empty BAM list | no inputs | create normalized BAM list first |
| very large VCF or slow run | whole-genome all-sample pileup is heavy | split by chromosome/region |
| no variants after filter | filter too strict or low coverage | inspect raw VCF and depth |
| unindexed VCF | downstream stats fail | run `bcftools index -t` |

Do not call a filtered VCF "final" until it is readable, indexed, and summarized.

## 相似的 skills

- `genomics/wgs-gatk-snp-indel` - high-confidence GATK GVCF workflow.
- `genomics/wgs-short-read-mapping` - create BAMs from FASTQ.
- `genomics/rnaseq-variant-calling` - RNA-seq variants.
- `genomics/snpeff-annotation` - annotate final SNP/InDel VCF.
