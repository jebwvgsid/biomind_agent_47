---
name: wgs-manta-sv
description: Primary short-read paired-end WGS structural variant and indel calling with Manta from coordinate-sorted
  indexed BAM or CRAM files. Use as the default short-read SV skill instead of LUMPY unless the user explicitly
  requests LUMPY or legacy split-read evidence.
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

# WGS Manta Structural Variant Calling

Use this as the default short-read paired-end SV caller. LUMPY is a legacy fallback, not the default.

## Task Boundary

Use this skill when:

- Input is paired-end WGS BAM/CRAM aligned to the target reference.
- BAM/CRAM files are coordinate sorted and indexed.
- The goal is short-read DEL, DUP, INV, BND/TRA, or candidate indel/SV discovery.

Do not use this skill for ONT/PacBio, read-depth-only CNV, RNA-seq by default, or assembly comparisons.

If the user provides FASTQ, run `genomics/wgs-short-read-mapping` first and then use the resulting sorted indexed BAM.

## Execution Guidance

Required:

| Parameter | Meaning |
|---|---|
| `REFERENCE_FASTA` | same reference used for alignment |
| `BAM` / `CRAM` | coordinate sorted and indexed |
| `OUTDIR` | output directory |
| `THREADS` | workflow cores |

Preflight:

```bash
samtools faidx "${REFERENCE_FASTA}"
samtools quickcheck -v "${BAM}"
configManta.py --help || configureManta.py --help
```

Germline diploid workflow:

```bash
configManta.py \
  --bam "${BAM}" \
  --referenceFasta "${REFERENCE_FASTA}" \
  --runDir "${OUTDIR}/03.variant_calling/${sample}.manta"

"${OUTDIR}/03.variant_calling/${sample}.manta/runWorkflow.py" \
  -m local -j "${THREADS:-8}"
```

If the installed package uses `configureManta.py`, use the same flags with that executable and record the version-specific command.

## Outputs and Validation

Expected outputs:

- `03.variant_calling/<sample>.manta/results/variants/diploidSV.vcf.gz`.
- `03.variant_calling/<sample>.manta/results/variants/candidateSV.vcf.gz`.
- `.tbi` indexes when produced by Manta.
- `05.summary/manta_sv_counts.tsv`.

Validation:

```bash
test -s "${OUTDIR}/03.variant_calling/${sample}.manta/results/variants/diploidSV.vcf.gz"
bcftools view -H "${OUTDIR}/03.variant_calling/${sample}.manta/results/variants/diploidSV.vcf.gz" |
  awk -F'\t' '{print $8}' | tr ';' '\n' | grep '^SVTYPE=' | sort | uniq -c \
  > "${OUTDIR}/05.summary/manta_sv_counts.tsv"
```

Do not mark success from a created run directory alone. Verify final VCFs and report no-call or blocked status explicitly.

## Common Problems

| Problem | Meaning | Fix |
|---|---|---|
| config script missing | Manta runtime unavailable | install or register `bio_manta`; do not fall back silently |
| BAM index missing | random access fails | run `samtools index` or block until provided |
| contig mismatch | BAM and reference builds differ | stop and ask/re-align |
| no candidate calls | possible true no-call or weak evidence | verify workflow completion and VCF header before reporting no_calls |

## Related Skills

- `genomics/wgs-short-read-mapping` - prepare BAMs from FASTQ.
- `genomics/wgs-lumpy-sv` - legacy LUMPY fallback when explicitly requested.
- `genomics/wgs-cnvnator-cnv` - read-depth CNV.
- `genomics/long-read-sv-sniffles2` - long-read SV.
