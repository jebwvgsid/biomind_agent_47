---
name: long-read-sv-sniffles2
description: Primary long-read structural variant calling with Sniffles2 from ONT, PacBio CLR, or PacBio HiFi FASTQ/BAM.
  Use as the default long-read SV skill unless the user explicitly asks for another caller or multi-caller validation.
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

# Long-Read SV with Sniffles2

This is the default primary long-read SV skill. Use one Sniffles2 workflow first; do not add cuteSV or pbsv unless the user requests multi-caller validation, PacBio-specific pbsv analysis, or Sniffles2 fails.

## Task Boundary

Use this skill when:

- Input is ONT, PacBio CLR, or PacBio HiFi FASTQ/BAM.
- The goal is DEL, INS, DUP, INV, BND/TRA, or general long-read SV discovery.
- The user wants a single default long-read SV caller.

Do not use this skill for short-read Manta/LUMPY SV, CNVnator CNV, RNA-seq variants, or assembly-level SV.

If only FASTQ is provided, first align with minimap2 using the correct preset:

| Platform | Preset |
|---|---|
| ONT | `map-ont` |
| PacBio CLR | `map-pb` |
| PacBio HiFi | `map-hifi` |

Ask the user only if the platform cannot be inferred from file names, metadata, or task text.

## Execution Guidance

Preflight:

```bash
sniffles --version
sniffles --help
samtools faidx "${REFERENCE_FASTA}"
```

FASTQ to BAM:

```bash
minimap2 -x "${PRESET}" -t "${THREADS:-16}" --MD --cs -a \
  "${REFERENCE_FASTA}" "${FASTQ}" |
samtools sort -@ "${THREADS:-16}" -o "${OUTDIR}/02.mapping/${sample}.sort.bam" -
samtools index "${OUTDIR}/02.mapping/${sample}.sort.bam"
```

Single-sample Sniffles2:

```bash
sniffles \
  --input "${BAM}" \
  --vcf "${OUTDIR}/03.variant_calling/${sample}.sniffles2.vcf" \
  --threads "${THREADS:-8}" \
  --minsvlen "${MIN_SV_LEN:-50}"
```

Optional SNF mode for cohort genotyping only when requested:

```bash
sniffles --input "${BAM}" --snf "${OUTDIR}/03.variant_calling/${sample}.snf" --threads "${THREADS:-8}"
sniffles --input "${OUTDIR}/03.variant_calling/"*.snf --vcf "${OUTDIR}/04.filter/cohort.sniffles2.vcf"
```

## Outputs and Validation

Expected outputs:

- `02.mapping/<sample>.sort.bam` and `.bai` when FASTQ was mapped.
- `03.variant_calling/<sample>.sniffles2.vcf`.
- optional `03.variant_calling/<sample>.snf`.
- `05.summary/sniffles2_sv_counts.tsv`.

Validation:

```bash
test -s "${OUTDIR}/03.variant_calling/${sample}.sniffles2.vcf"
grep -v '^#' "${OUTDIR}/03.variant_calling/${sample}.sniffles2.vcf" |
  awk -F'\t' '{print $8}' | tr ';' '\n' | grep '^SVTYPE=' | sort | uniq -c \
  > "${OUTDIR}/05.summary/sniffles2_sv_counts.tsv"
```

A zero-byte VCF or missing BAM index is not success. An empty but valid VCF may be reported as `no_calls` only after the command completed and the VCF header exists.

## Common Problems

| Problem | Meaning | Fix |
|---|---|---|
| Sniffles reports unknown option | installed Sniffles is not v2 syntax | inspect `sniffles --help`; use this skill only with v2-compatible syntax |
| too few SVs | low depth, high support threshold, poor mapping | inspect depth/read length and tune support |
| mixed references | cohort comparison invalid | re-align to one reference |
| platform unclear | wrong minimap2 preset risk | ask user before mapping |

## Related Skills

- `genomics/long-read-sv-cutesv` - optional cross-caller validation.
- `genomics/pacbio-sv-pbsv` - optional PacBio pbsv workflow.
- `genomics/wgs-manta-sv` - short-read SV.
- `genomics/assembly-variant-syri` - assembly-level SV.
