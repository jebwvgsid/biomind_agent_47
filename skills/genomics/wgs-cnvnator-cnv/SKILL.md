---
name: wgs-cnvnator-cnv
description: Detect read-depth copy-number variants from short-read WGS BAMs with CNVnator, including bin-size selection,
  histogram/stat/eval/partition/call steps, optional VCF conversion, filtering, and CNV summaries. Use for deletion/duplication
  CNV screening from resequencing BAMs. Do not use for RNA-seq, long-read SV, or split-read SV-only analysis.
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

# WGS CNVnator CNV Calling

Use this skill for read-depth based CNV detection from short-read WGS BAMs. CNVnator is per-sample by design; preserve sample identity when summarizing.

## 确认任务边界

Use this skill when:

- Input is WGS BAM aligned to the same reference.
- The biological goal is copy-number deletions/duplications from read depth.
- The user accepts resolution limits determined by depth and bin size.

Do not use this skill when:

- Input is RNA-seq.
- The goal is short-read SV; use Manta by default, or LUMPY only if explicitly requested.
- The data are ONT/PacBio long reads; use long-read SV tools.

CNVnator calls are screening results; treat small CNVs, low-depth samples, and repetitive regions cautiously.

## 调用的参数

Required:

| Parameter | Meaning |
|---|---|
| `REFERENCE_FASTA` | same reference used for BAM alignment |
| `BAM` or `SAMPLE_BAM_TABLE` | sorted indexed BAMs |
| `BIN_SIZE` | read-depth bin size |
| `OUTDIR` | output directory |

Bin-size rule:

- Do not assume tutorial values such as `500` are universal.
- If no bin size is provided, test a small set such as `100`, `500`, `1000`, or choose based on genome size, depth, and desired CNV resolution.
- Larger bins improve stability but reduce resolution.

Preflight:

```bash
cnvnator -h
samtools quickcheck -v "${BAM}"
test -s "${REFERENCE_FASTA}.fai" || samtools faidx "${REFERENCE_FASTA}"
```

Prepare chromosome FASTA files:

```bash
mkdir -p "${OUTDIR}/01.ref/split"
seqkit split -i "${REFERENCE_FASTA}" -O "${OUTDIR}/01.ref/split"
```

Core per-sample workflow:

```bash
cnvnator -genome "${REFERENCE_FASTA}" \
  -root "${OUTDIR}/03.variant_calling/${sample}.root" \
  -tree "${BAM}"

cnvnator -genome "${REFERENCE_FASTA}" \
  -root "${OUTDIR}/03.variant_calling/${sample}.root" \
  -his "${BIN_SIZE}" \
  -d "${OUTDIR}/01.ref/split"

cnvnator -root "${OUTDIR}/03.variant_calling/${sample}.root" -stat "${BIN_SIZE}" \
  > "${OUTDIR}/05.summary/${sample}.stat.${BIN_SIZE}.txt"
cnvnator -root "${OUTDIR}/03.variant_calling/${sample}.root" -eval "${BIN_SIZE}" \
  > "${OUTDIR}/05.summary/${sample}.eval.${BIN_SIZE}.txt"
cnvnator -root "${OUTDIR}/03.variant_calling/${sample}.root" -partition "${BIN_SIZE}"
cnvnator -root "${OUTDIR}/03.variant_calling/${sample}.root" -call "${BIN_SIZE}" \
  > "${OUTDIR}/03.variant_calling/${sample}.cnv.${BIN_SIZE}.txt"
```

Recommended native filtering:

- q0 `< 0.5`.
- e-val1 or CNVnatorP1 `< 0.05`.
- Report thresholds and do not hide raw calls.

## 输出结果

Expected outputs:

- `03.variant_calling/<sample>.root`
- `03.variant_calling/<sample>.cnv.<bin>.txt`
- `04.filter/<sample>.cnv.filtered.<bin>.tsv`
- optional `04.filter/<sample>.cnv.<bin>.vcf`
- `05.summary/<sample>.stat.<bin>.txt`
- `05.summary/<sample>.eval.<bin>.txt`
- `05.summary/cnv_counts.tsv`
- `logs/*.log`

Validation:

```bash
test -s "${OUTDIR}/03.variant_calling/${sample}.cnv.${BIN_SIZE}.txt"
awk 'NF>=9' "${OUTDIR}/03.variant_calling/${sample}.cnv.${BIN_SIZE}.txt" | head
cat "${OUTDIR}/05.summary/${sample}.eval.${BIN_SIZE}.txt"
```

Report CNV counts by type and bin size, total called length, and any bin-size warnings.

## 常见错误及解决办法

| Problem | Meaning | Fix |
|---|---|---|
| ROOT/CNVnator missing | environment incomplete | activate/install CNVnator environment |
| no histogram | split FASTA directory wrong | verify chromosome FASTA files in `-d` directory |
| unstable RD signal | bin size too small or depth too low | test larger bin size |
| too coarse CNVs | bin size too large | test smaller bin size if depth supports it |
| raw calls merged across samples | sample identity lost | keep per-sample files and add sample column |

## 相似的 skills

- `genomics/wgs-manta-sv` - default short-read SV.
- `genomics/wgs-lumpy-sv` - legacy split-read/discordant-pair SV fallback.
- `genomics/long-read-sv-sniffles2` - long-read SV.
- `genomics/wgs-short-read-mapping` - prepare BAMs.
- `genomics/assembly-variant-syri` - assembly-level structural variation.
