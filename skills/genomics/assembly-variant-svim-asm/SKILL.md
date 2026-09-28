---
name: assembly-variant-svim-asm
description: Optional assembly-level structural variant calling with SVIM-asm from minimap2 assembly-to-reference
  BAM alignments. Use when the user explicitly requests SVIM-asm or SyRI is not suitable for the assembly input.
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

# Optional Assembly SV with SVIM-asm

Use SVIM-asm as an optional assembly-level caller. It is not the default when SyRI input requirements are satisfied.

## Execution Guidance

Create assembly-to-reference BAM if needed:

```bash
minimap2 -a -x "${ASM_PRESET:-asm5}" --cs -r2k \
  "${REFERENCE_ASSEMBLY}" "${QUERY_ASSEMBLY}" |
samtools sort -@ "${THREADS:-8}" -o "${OUTDIR}/03.variant_calling/${sample}.asm.bam" -
samtools index "${OUTDIR}/03.variant_calling/${sample}.asm.bam"
```

Run haploid mode for one query assembly:

```bash
svim-asm haploid \
  "${OUTDIR}/04.filter/${sample}.svim_asm" \
  "${OUTDIR}/03.variant_calling/${sample}.asm.bam" \
  "${REFERENCE_ASSEMBLY}"
```

Use diploid mode only when two haplotype assemblies are explicitly provided.

## Outputs and Validation

- `04.filter/<sample>.svim_asm/variants.vcf`.
- `05.summary/svim_asm_sv_counts.tsv`.

```bash
test -s "${OUTDIR}/04.filter/${sample}.svim_asm/variants.vcf"
grep -v '^#' "${OUTDIR}/04.filter/${sample}.svim_asm/variants.vcf" |
  awk -F'\t' '{print $8}' | tr ';' '\n' | grep '^SVTYPE=' | sort | uniq -c \
  > "${OUTDIR}/05.summary/svim_asm_sv_counts.tsv"
```

Report that SVIM-asm is assembly-alignment evidence, not read-depth or paired-end read evidence.
