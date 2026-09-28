---
name: wgs-lumpy-sv
description: Legacy/fallback short-read WGS structural variant calling with LUMPY, svtyper, and svtools from paired-end
  BAM evidence including discordant pairs and split reads. Prefer genomics/wgs-manta-sv as the default short-read
  SV skill unless the user explicitly requests LUMPY or a legacy project requires it.
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

# Legacy WGS LUMPY Structural Variant Calling

Use this skill only as a legacy/fallback short-read SV caller. For ordinary paired-end WGS SV requests, prefer `genomics/wgs-manta-sv`. LUMPY environments are often fragile; verify tools before any long run.

## 确认任务边界

Use this skill when:

- Input BAMs are paired-end short-read WGS.
- BAMs are coordinate sorted and indexed.
- The user explicitly requests LUMPY, split-read/discordant-pair LUMPY evidence, or a legacy workflow that already depends on LUMPY.

Do not use this skill when:

- The user asks vaguely for short-read SV; use `genomics/wgs-manta-sv`.
- Data are ONT/PacBio; use `genomics/long-read-sv-sniffles2`.
- The goal is read-depth CNV only; use `genomics/wgs-cnvnator-cnv`.
- BAMs are RNA-seq or single-end reads.

Warn that short-read SV calling is sensitive to library insert size, mapping quality, repetitive regions, and coverage.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `BAM_LIST` | `sample<TAB>bam` preferred |
| `OUTDIR` | output directory |

Recommended:

| Parameter | Default / notes |
|---|---|
| `THREADS` | 4-8 for extraction/sorting |
| `MIN_SUPPORT` | caller/environment-specific |
| `TMPDIR` | needed by svtools |
| `REFERENCE_FASTA` | for compatibility checks and downstream annotation |

Preflight:

```bash
lumpyexpress -h
svtyper --help
svtools --help
samtools quickcheck -v $(cut -f2 "${BAM_LIST}")
```

Per-sample evidence extraction and discovery:

```bash
samtools view -b -F 1294 -@ "${THREADS:-4}" "${bam}" \
  > "${OUTDIR}/03.variant_calling/${sample}.discordants.bam"
samtools index "${OUTDIR}/03.variant_calling/${sample}.discordants.bam"

samtools view -h "${bam}" |
extractSplitReads_BwaMem -i stdin |
samtools sort -@ "${THREADS:-4}" \
  -o "${OUTDIR}/03.variant_calling/${sample}.splitters.bam" -
samtools index "${OUTDIR}/03.variant_calling/${sample}.splitters.bam"

lumpyexpress \
  -P \
  -B "${bam}" \
  -S "${OUTDIR}/03.variant_calling/${sample}.splitters.bam" \
  -D "${OUTDIR}/03.variant_calling/${sample}.discordants.bam" \
  -o "${OUTDIR}/03.variant_calling/${sample}.lumpy.vcf"

svtyper \
  -i "${OUTDIR}/03.variant_calling/${sample}.lumpy.vcf" \
  -B "${bam}" \
  -o "${OUTDIR}/03.variant_calling/${sample}.lumpy.gt.vcf"
```

Cohort merge pattern:

```bash
find "${OUTDIR}/03.variant_calling" -name "*.lumpy.gt.vcf" | sort > "${OUTDIR}/03.variant_calling/vcf.list"
svtools lsort -f "${OUTDIR}/03.variant_calling/vcf.list" -t "${TMPDIR}" \
  > "${OUTDIR}/03.variant_calling/all.lsort.vcf"
svtools lmerge -i "${OUTDIR}/03.variant_calling/all.lsort.vcf" -t "${TMPDIR}" \
  > "${OUTDIR}/03.variant_calling/all.lmerge.vcf"
```

Re-genotype merged sites per sample before final cohort reporting.

## 输出结果

Expected outputs:

- `03.variant_calling/<sample>.discordants.bam`
- `03.variant_calling/<sample>.splitters.bam`
- `03.variant_calling/<sample>.lumpy.vcf`
- `03.variant_calling/<sample>.lumpy.gt.vcf`
- `03.variant_calling/all.lmerge.vcf`
- `04.filter/all.genotype.sv.vcf`
- `05.summary/sv_type_counts.tsv`
- `logs/*.log`

Validation:

```bash
test -s "${OUTDIR}/04.filter/all.genotype.sv.vcf"
grep -v '^#' "${OUTDIR}/04.filter/all.genotype.sv.vcf" |
  awk -F'\t' '{print $8}' | tr ';' '\n' | grep '^SVTYPE=' | sort | uniq -c \
  > "${OUTDIR}/05.summary/sv_type_counts.tsv"
```

Report SVTYPE counts, sample count, genotyping completeness, and any filtered/failed samples.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| `extractSplitReads_BwaMem` missing | LUMPY helper not in PATH | activate correct LUMPY environment |
| very few split/discordant reads | BAM not paired-end, low coverage, strict flags | inspect library metrics and extraction commands |
| `svtools vcfpaste` header errors | sample headers incompatible | normalize sample names before merging |
| ungenotyped VCF used as final | discovery-only calls | run svtyper on merged sites |
| old Python errors | LUMPY/svtools version conflict | use pinned environment and report blocker |

## 相似的 skills

- `genomics/wgs-manta-sv` - default short-read SV.
- `genomics/long-read-sv-sniffles2` - long-read SV.
- `genomics/wgs-cnvnator-cnv` - read-depth CNV.
- `genomics/wgs-short-read-mapping` - prepare paired-end BAMs.
- `genomics/assembly-variant-syri` - assembly-based SV and synteny.
