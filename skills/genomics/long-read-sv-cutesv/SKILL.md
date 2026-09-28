---
name: long-read-sv-cutesv
description: Optional long-read structural variant calling with cuteSV for ONT, PacBio CLR, or PacBio HiFi BAMs.
  Use for multi-caller validation, benchmarking, or Sniffles2 failure follow-up, not as the default long-read SV
  route.
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

# Optional Long-Read SV with cuteSV

Use cuteSV as an optional cross-caller. Do not automatically add it to ordinary long-read SV plans.

## Execution Guidance

Input must be a coordinate-sorted indexed long-read BAM aligned to the same reference.

```bash
samtools quickcheck -v "${BAM}"
samtools faidx "${REFERENCE_FASTA}"
mkdir -p "${OUTDIR}/03.variant_calling/${sample}.cutesv.work"
cuteSV "${BAM}" "${REFERENCE_FASTA}" \
  "${OUTDIR}/03.variant_calling/${sample}.cutesv.vcf" \
  "${OUTDIR}/03.variant_calling/${sample}.cutesv.work" \
  --threads "${THREADS:-8}"
```

Tune platform-specific options only after checking the installed `cuteSV --help` and documenting the selected preset.

## Outputs and Validation

- `03.variant_calling/<sample>.cutesv.vcf`.
- `05.summary/cutesv_sv_counts.tsv`.

```bash
test -s "${OUTDIR}/03.variant_calling/${sample}.cutesv.vcf"
grep -v '^#' "${OUTDIR}/03.variant_calling/${sample}.cutesv.vcf" |
  awk -F'\t' '{print $8}' | tr ';' '\n' | grep '^SVTYPE=' | sort | uniq -c \
  > "${OUTDIR}/05.summary/cutesv_sv_counts.tsv"
```

If the output VCF is empty or malformed, report the caller as blocked/unresolved rather than converting it into biological no-hit/no-call evidence.
