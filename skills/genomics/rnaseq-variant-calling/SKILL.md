---
name: rnaseq-variant-calling
description: Call SNPs and InDels from RNA-seq reads using splice-aware alignment, duplicate marking, read groups,
  GATK SplitNCigarReads, HaplotypeCaller, and RNA-seq-specific hard filtering. Use only when the user explicitly
  wants transcriptome-based variant detection. Do not use for expression-only RNA-seq analysis or WGS variant calling.
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

# RNA-seq Variant Calling

Use this skill only for variants from RNA-seq. RNA-seq variant calls are limited to expressed regions and are affected by expression bias, splicing, RNA editing, allele-specific expression, and mapping around exon junctions.

## 确认任务边界

Use this skill when:

- The user explicitly asks for SNP/InDel calling from RNA-seq.
- Input is RNA-seq FASTQ or splice-aware RNA-seq BAM.
- The goal is candidate expressed variants, not whole-genome completeness.

Do not use this skill when:

- The user wants DE analysis, transcript quantification, or pathway analysis; use RNA-seq expression skills.
- Input is WGS; use WGS SNP/InDel skills.
- The target is SV/CNV.

State in the report that absence of a variant in RNA-seq does not mean absence in genome.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `REFERENCE_FASTA` | FASTA with `.fai` and `.dict` |
| `SAMPLE_TABLE` | `sample fq1 fq2` or existing RNA-seq BAM list |
| `OUTDIR` | output directory |

Recommended:

| Parameter | Default / notes |
|---|---|
| `ALIGNER` | HISAT2 for this skill's default; STAR is acceptable if already used |
| `ANNOTATION_GTF/GFF3` | improves splice-aware alignment when available |
| `THREADS` | 8-16 for alignment |
| `MEMORY` | GATK Java memory |
| `TMPDIR` | local scratch |

Default HISAT2 mapping:

```bash
hisat2-build "${REFERENCE_FASTA}" "${OUTDIR}/01.ref/hisat2_index/ref"
hisat2 -x "${OUTDIR}/01.ref/hisat2_index/ref" \
  -1 "${fq1}" -2 "${fq2}" \
  -p "${THREADS:-8}" --new-summary |
samtools sort -@ "${THREADS:-8}" \
  -o "${OUTDIR}/02.mapping/${sample}.sort.bam" -
```

Duplicate marking and read groups:

```bash
picard MarkDuplicates \
  I="${OUTDIR}/02.mapping/${sample}.sort.bam" \
  O="${OUTDIR}/02.mapping/${sample}.sort.markdup.bam" \
  M="${OUTDIR}/02.mapping/${sample}.markdup.metrics.txt" \
  CREATE_INDEX=true \
  REMOVE_DUPLICATES=false

picard AddOrReplaceReadGroups \
  I="${OUTDIR}/02.mapping/${sample}.sort.markdup.bam" \
  O="${OUTDIR}/02.mapping/${sample}.sort.markdup.RG.bam" \
  RGID="${sample}" RGLB="${sample}" RGPL=illumina RGPU="${sample}" RGSM="${sample}" \
  CREATE_INDEX=true
```

Required GATK splice handling:

```bash
gatk --java-options "-Xmx${MEMORY} -Djava.io.tmpdir=${TMPDIR}" SplitNCigarReads \
  -R "${REFERENCE_FASTA}" \
  -I "${OUTDIR}/02.mapping/${sample}.sort.markdup.RG.bam" \
  -O "${OUTDIR}/02.mapping/${sample}.splitncigar.bam"

gatk HaplotypeCaller \
  -R "${REFERENCE_FASTA}" \
  -I "${OUTDIR}/02.mapping/${sample}.splitncigar.bam" \
  --dont-use-soft-clipped-bases \
  -O "${OUTDIR}/03.variant_calling/${sample}.raw.vcf.gz"
```

RNA-seq hard filter starting point:

```bash
gatk VariantFiltration \
  -R "${REFERENCE_FASTA}" \
  -V "${OUTDIR}/03.variant_calling/all.raw.vcf.gz" \
  --filter-expression "QD < 2.0 || FS > 30.0 || SOR > 3.0" \
  --filter-name RNAseq_filter \
  -O "${OUTDIR}/04.filter/all.filter.vcf.gz"
```

## 输出结果

Expected outputs:

- `02.mapping/<sample>.sort.markdup.RG.bam`
- `02.mapping/<sample>.splitncigar.bam`
- `03.variant_calling/<sample>.raw.vcf.gz` or cohort raw VCF.
- `04.filter/all.filtered.snp.vcf.gz`
- `04.filter/all.filtered.indel.vcf.gz`
- `05.summary/rnaseq_variant_counts.tsv`
- `logs/*.hisat2.log`, `logs/*.HaplotypeCaller.log`

Validation:

```bash
samtools quickcheck -v "${OUTDIR}/02.mapping/"*.splitncigar.bam
samtools view -H "${OUTDIR}/02.mapping/${sample}.splitncigar.bam" | grep '^@RG'
bcftools stats "${OUTDIR}/04.filter/all.filtered.snp.vcf.gz" > "${OUTDIR}/05.summary/rnaseq.snp.stats.txt"
```

Report mapping rate, expressed-region limitation, filtered SNP/InDel counts, and filter expressions.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| `SplitNCigarReads` skipped | splice junction CIGARs not normalized | run it before HaplotypeCaller |
| no read group | GATK sample metadata missing | add/replace read groups |
| low mapping rate | wrong reference, contamination, poor reads | inspect RNA-seq QC and annotation |
| many false-looking variants near junctions | RNA-seq mapping artifact | filter cautiously and inspect BAM |
| user expects genome-wide calls | RNA-seq only covers expressed regions | explain limitation and suggest WGS |

## 相似的 skills

- `genomics/wgs-gatk-snp-indel` - WGS GATK SNP/InDel.
- `genomics/wgs-bcftools-snp-indel` - lightweight WGS SNP/InDel.
- `crop-breeding/crop-rnaseq-pipeline` - expression analysis, not variant calling.
- `crop-breeding/crop-rnaseq-de` - differential expression.
- `genomics/snpeff-annotation` - annotate final VCF.
