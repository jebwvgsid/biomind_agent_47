---
name: assembly-variant-syri
description: Detect synteny-aware variants between two chromosome-level assemblies using MUMmer/nucmer alignment
  and SyRI, with optional plotsr visualization. Use when users provide reference and query assemblies and ask for
  assembly-based SNP, InDel, inversion, translocation, duplication, syntenic regions, or structural variation. Do
  not use on FASTQ/BAM read data.
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

# Assembly Variant Calling with SyRI

Use this as the default assembly-level SV/synteny skill for pairwise comparison of chromosome-level assemblies. SyRI interpretation depends strongly on assembly contiguity and chromosome correspondence. Assemblytics and SVIM-asm are optional supplemental callers, not default stages.

## 确认任务边界

Use this skill when:

- The user provides exactly two assemblies: reference and query.
- Assemblies are chromosome-level or close enough for synteny interpretation.
- The goal is assembly-based SNP, InDel, inversion, translocation, duplication, or syntenic/non-syntenic region detection.

Do not use this skill when:

- Input is raw FASTQ/BAM.
- Assemblies are highly fragmented scaffolds and the user expects chromosome-scale SV conclusions.
- The two FASTA files are the same assembly.

By default, exclude organellar genomes, unplaced scaffolds, and very small contigs unless the user requests them.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `REFERENCE_ASSEMBLY` | reference FASTA |
| `QUERY_ASSEMBLY` | query FASTA |
| `OUTDIR` | output directory |

Recommended:

| Parameter | Default / notes |
|---|---|
| `MIN_CLUSTER` | 100 for small genomes; increase for large repeats |
| `BREAK_LENGTH` | 500 |
| `MIN_EXACT` | 50 |
| `IDENTITY` | 90 starting point; tune by divergence |
| `MIN_ALIGN_LEN` | 100 or higher for large genomes |

Preflight:

```bash
nucmer --version
delta-filter -h
show-coords -h
syri --help
samtools faidx "${REFERENCE_ASSEMBLY}"
samtools faidx "${QUERY_ASSEMBLY}"
```

Whole-genome alignment:

```bash
mkdir -p "${OUTDIR}/03.variant_calling" "${OUTDIR}/04.filter" "${OUTDIR}/05.summary" "${OUTDIR}/logs"
nucmer --maxmatch \
  -c "${MIN_CLUSTER:-100}" \
  -b "${BREAK_LENGTH:-500}" \
  -l "${MIN_EXACT:-50}" \
  -p "${OUTDIR}/03.variant_calling/out" \
  "${REFERENCE_ASSEMBLY}" "${QUERY_ASSEMBLY}" \
  > "${OUTDIR}/logs/nucmer.log" 2>&1
```

Filter and convert:

```bash
delta-filter -m \
  -i "${IDENTITY:-90}" \
  -l "${MIN_ALIGN_LEN:-100}" \
  "${OUTDIR}/03.variant_calling/out.delta" \
  > "${OUTDIR}/03.variant_calling/out.filtered.delta"

show-coords -T -H -r -d \
  "${OUTDIR}/03.variant_calling/out.filtered.delta" \
  > "${OUTDIR}/03.variant_calling/out.filtered.coords"
```

Run SyRI:

```bash
syri \
  -c "${OUTDIR}/03.variant_calling/out.filtered.coords" \
  -d "${OUTDIR}/03.variant_calling/out.filtered.delta" \
  -r "${REFERENCE_ASSEMBLY}" \
  -q "${QUERY_ASSEMBLY}" \
  --prefix "${OUTDIR}/04.filter/syri" \
  > "${OUTDIR}/logs/syri.log" 2>&1
```

If `syri --help` shows different prefix/output options, adapt and record the exact command.

Optional plot:

```bash
plotsr \
  "${OUTDIR}/04.filter/syri.out" \
  "${REFERENCE_ASSEMBLY}" \
  "${QUERY_ASSEMBLY}" \
  -H 8 -W 5 \
  -o "${OUTDIR}/05.summary/syri.pdf"
```

## 输出结果

Expected outputs:

- `03.variant_calling/out.delta`
- `03.variant_calling/out.filtered.delta`
- `03.variant_calling/out.filtered.coords`
- `04.filter/syri.out`
- `04.filter/syri.vcf`
- `04.filter/syri.summary`
- `05.summary/syri_variant_counts.tsv`
- optional `05.summary/syri.pdf`
- `logs/nucmer.log`, `logs/syri.log`

Validation:

```bash
test -s "${OUTDIR}/03.variant_calling/out.filtered.coords"
test -s "${OUTDIR}/04.filter/syri.out"
test -s "${OUTDIR}/04.filter/syri.vcf"
awk '{print $11}' "${OUTDIR}/04.filter/syri.out" | sort | uniq -c \
  > "${OUTDIR}/05.summary/syri_variant_counts.tsv"
```

Report assembly names, N50/contig level if known, alignment filtering thresholds, and variant category counts.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| no filtered alignments | thresholds too strict or wrong files | lower filters or verify assemblies |
| many tiny chaotic SVs | fragmented assemblies/repeats | report preliminary and filter to chromosome-level contigs |
| organelle/unplaced contigs dominate | wrong FASTA scope | build chromosome-only FASTA |
| SyRI coordinate errors | coords/delta generated with incompatible options | regenerate with documented `show-coords` flags |
| plotsr syntax differs | version mismatch | inspect `plotsr --help` and record command |

## 相似的 skills

- `genomics/assembly-variant-assemblytics` - optional delta-based assembly SV summary.
- `genomics/assembly-variant-svim-asm` - optional assembly-alignment SV caller.
- `genomics/long-read-sv-sniffles2` - read-based long-read SV.
- `genomics/wgs-manta-sv` - short-read SV.
- `genomics/wgs-cnvnator-cnv` - read-depth CNV.
- `genomics/assembly` - genome assembly before comparison.
