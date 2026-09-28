---
name: dorado-modbase-prep
description: 'Prepare Oxford Nanopore 5mC/5hmC modBAM inputs for modkit by running Dorado modified-base basecalling
  from POD5/FAST5 data or aligning existing modified basecalls while preserving MM/ML tags. Use when the user has
  POD5, FAST5, unaligned modBAM, or ordinary basecalling outputs and asks for 5mC methylation detection before modkit
  pileup. Produces aligned, coordinate-sorted, indexed modBAM suitable for nanopore-modkit-pileup. Do not use for
  DMR once bedMethyl already exists.

  '
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

# Dorado Modified-Base Prep For Modkit

## Scope

Use this optional upstream skill when the user does not yet have an aligned sorted indexed modBAM with `MM`/`ML` tags.

Reference facts checked from upstream documentation:

- Dorado modified basecalling extends simplex/duplex basecalling.
- Dorado writes modified-base calls into SAM/BAM `MM`/`ML` tags.
- DNA modified bases include 5mC (`C+m`), 5hmC (`C+h`), 4mC, and 6mA.
- All-context `5mC` models predict at all cytosines; motif models such as `5mCG` predict only in motif context.
- Only one modification model per canonical base can be active at once.
- Dorado can align during basecalling with `--reference`, or align existing calls with `dorado aligner`.

Sources: https://github.com/nanoporetech/dorado, https://software-docs.nanoporetech.com/dorado/latest/basecaller/mods/.

## Required Inputs

- `READS`: POD5/FAST5 directory or file, or unaligned BAM with modified-base tags.
- `REFERENCE_FASTA`: target reference FASTA or minimap2 index accepted by Dorado.
- `SAMPLE`: sample name.
- `OUTDIR`: output directory.
- `MODEL`: basecalling model such as `hac`, `sup`, or a full model name/path.
- `MODS`: default `5mC` for plant all-cytosine methylation. Use `5mCG` or `5mCG_5hmCG` only when CpG-only is intended.
- Optional `DEVICE`: e.g. `cuda:all`, `cuda:0`, `cpu`, or `metal`.

## Model Selection Rules

| Biological question | Dorado modification choice |
|---|---|
| Plant/crop CG/CHG/CHH 5mC | all-context `5mC` |
| Mammalian CpG-only 5mC | `5mCG` |
| Distinguish 5mC and 5hmC at CpG | `5mCG_5hmCG` |
| Distinguish all-context 5mC and 5hmC | `5mC_5hmC` if compatible with the selected model |

Do not combine two cytosine modification models in one Dorado run, for example `4mC_5mC` and `5mC_5hmC`, because Dorado permits only one modification model per canonical base at a time.

## Basecall And Align In One Step

Preferred when starting from POD5 and a reference is available:

```bash
mkdir -p "${OUTDIR}/bam" "${OUTDIR}/logs" "${OUTDIR}/qc"
dorado basecaller "${MODEL}" "${READS}" \
  --modified-bases "${MODS:-5mC}" \
  --reference "${REFERENCE_FASTA}" \
  --device "${DEVICE:-cuda:all}" \
  > "${OUTDIR}/bam/${SAMPLE}.aligned.unsorted.bam"
samtools sort -@ "${THREADS:-8}" \
  -o "${OUTDIR}/bam/${SAMPLE}.modbase.sorted.bam" \
  "${OUTDIR}/bam/${SAMPLE}.aligned.unsorted.bam"
samtools index "${OUTDIR}/bam/${SAMPLE}.modbase.sorted.bam"
```

If the installed Dorado version expects a comma-separated model string instead of `--modified-bases`, use:

```bash
dorado basecaller "${MODEL},${MODS}" "${READS}" --reference "${REFERENCE_FASTA}" > calls.bam
```

Prefer checking `dorado basecaller --help` before choosing syntax on an unfamiliar cluster.

## Align Existing Unaligned modBAM

Use this when the BAM already contains `MM`/`ML` tags but is not aligned:

```bash
dorado aligner "${REFERENCE_FASTA}" "${UNALIGNED_MODBAM}" \
  > "${OUTDIR}/bam/${SAMPLE}.aligned.unsorted.bam"
samtools sort -@ "${THREADS:-8}" \
  -o "${OUTDIR}/bam/${SAMPLE}.modbase.sorted.bam" \
  "${OUTDIR}/bam/${SAMPLE}.aligned.unsorted.bam"
samtools index "${OUTDIR}/bam/${SAMPLE}.modbase.sorted.bam"
```

Do not convert through FASTQ if modified-base tags need to be preserved.

## QC And Handoff To Modkit

Generate a Dorado read summary:

```bash
dorado summary "${OUTDIR}/bam/${SAMPLE}.modbase.sorted.bam" \
  > "${OUTDIR}/qc/${SAMPLE}.dorado_summary.tsv"
```

Check modified-base tags:

```bash
modkit modbam check-tags "${OUTDIR}/bam/${SAMPLE}.modbase.sorted.bam" \
  --head 1000 \
  --log "${OUTDIR}/logs/${SAMPLE}.check_tags.log" \
  > "${OUTDIR}/qc/${SAMPLE}.check_tags.txt"
```

Expected tags:

| Modification | SAM tag/code |
|---|---|
| 5mC | `C+m` |
| 5hmC | `C+h` |

If the tag check passes and the BAM is indexed, hand off to `nanopore-modkit-pileup`.

## Reporting Rules

Report:

- Dorado version and selected model/modification syntax.
- whether the model was all-context or motif-limited.
- reference used for alignment.
- sorted BAM and index paths.
- `check-tags` result and whether `C+m` is present.

Warn the user when:

- They used `5mCG` but later ask for CHG/CHH methylation; those contexts were never called.
- They provide only FASTQ without modified-base tags; FASTQ cannot recover methylation probabilities.
- They request CPU basecalling on large POD5 datasets; it may be impractically slow.
