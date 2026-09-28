---
name: wgs-bcftools-snp-indel
description: Call short-read WGS SNPs and InDels with bcftools mpileup and call, then filter, split SNP/InDel VCFs,
  index, and summarize. Use for fast lightweight resequencing variant calling on small or medium cohorts when GATK
  GVCF joint genotyping is unnecessary or unavailable. Do not present it as equivalent to the GATK workflow for
  high-confidence large cohorts.
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
