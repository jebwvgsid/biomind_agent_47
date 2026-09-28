---
name: reseq-variant-workflow
description: Route resequencing variant analysis across short-read WGS SNP/InDel, short-read SV, read-depth CNV,
  RNA-seq variant calling, long-read SV, and assembly-vs-assembly SyRI workflows. Use when the user asks for 重测序变异检测,
  SNP, InDel, SV, CNV, RNA-seq variants, long-read structural variants, or assembly-based variants and BioMind must
  choose the right primary downstream skill instead of running every method.
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

# Resequencing Variant Workflow

Use this skill as the top-level controller. Its main job is to classify the user's data and biological goal, then select only the necessary downstream skills. Do not run SNP/InDel, SV, CNV, RNA-seq, long-read, and assembly comparison together unless the user explicitly requests a multi-modal project.

For SV analysis, default to one scientifically appropriate primary workflow. Extra callers are optional validation, not default work.

## 确认任务边界

First classify both **data type** and **variant class**:

| User data / goal | Use downstream skill |
|---|---|
| Paired-end short-read WGS FASTQ needing mapping | `genomics/wgs-short-read-mapping` |
| Short-read WGS SNP/InDel, high-confidence cohort | `genomics/wgs-gatk-snp-indel` or existing `genomics/wgs-variant-calling` |
| Short-read WGS SNP/InDel, lightweight/small cohort | `genomics/wgs-bcftools-snp-indel` |
| Short-read WGS deletion/duplication/inversion/translocation SV | `genomics/wgs-manta-sv` |
| Short-read WGS read-depth CNV | `genomics/wgs-cnvnator-cnv` |
| RNA-seq SNP/InDel from transcriptome reads | `genomics/rnaseq-variant-calling` |
| ONT/PacBio SV from long-read FASTQ/BAM | `genomics/long-read-sv-sniffles2` |
| Two chromosome-level assemblies | `genomics/assembly-variant-syri` |
| Functional annotation of final VCF | `genomics/snpeff-annotation` |

SV caller selection defaults:

- Long-read ONT/PacBio/unknown platform: select `genomics/long-read-sv-sniffles2` first.
- PacBio projects: select `genomics/pacbio-sv-pbsv` only when the user asks for pbsv/PacBio official workflow or the project specifically requires it.
- Extra long-read caller validation: select `genomics/long-read-sv-cutesv` only when the user requests multiple callers, benchmarking/cross-caller validation, or Sniffles2 fails.
- Short-read paired-end SV: select `genomics/wgs-manta-sv` first. Use `genomics/wgs-lumpy-sv` only when the user explicitly requests LUMPY, a legacy project requires it, or Manta is unavailable and the user accepts fallback.
- Assembly-level SV/synteny: select `genomics/assembly-variant-syri` first. Use Assemblytics or SVIM-asm only when the user explicitly asks for supplemental assembly callers or the SyRI input contract is not suitable.

Default behavior when the request is vague:

- If the user says only "重测序变异检测" and provides short-read WGS FASTQ/BAM, plan WGS SNP/InDel first.
- Mention that SV and CNV are separate analyses with different evidence models.
- Ask for variant class only when choosing wrongly would waste substantial compute or change biological interpretation.
- If the user says only "SV calling" and data type is known, choose the single default primary caller for that data type. Do not expand to Sniffles2 + cuteSV + pbsv + Manta + SyRI unless the user requests a multi-caller or multi-modal comparison.

Do not use this workflow for de novo assembly, GWAS, population genetics after VCF generation, or methylation analysis.

## 调用的参数

New short-read FASTQ preflight candidates should inspect and reuse
`genomics/wgs-short-read-mapping/scripts/inspect_fastq_batch.py`: exact bound
items, parent-level live progress, one merged formal observation, and an
adjustable 60-second initial per-item limit, starting after approval. Keep the
existing outer execution deadline and reader coverage limits. Preserve child
stdout/stderr in execution logs, not repeated formal markers; timeout and
cancelled/unstarted items remain UNKNOWN. Never extend this short-check policy
to long scientific stages or rewrite an old approved candidate.

Collect or infer these before selecting an execution skill:

| Parameter | Required for |
|---|---|
| `REFERENCE_FASTA` | all read-based workflows |
| `SAMPLE_TABLE` | batch FASTQ/BAM workflows |
| `DATA_TYPE` | WGS short read, RNA-seq, ONT, PacBio CLR, HiFi, assemblies |
| `VARIANT_CLASS` | SNP/InDel, SV, CNV, all requested classes |
| `PLOIDY` | GATK interpretation, filtering, genotype assumptions |
| `THREADS`, `MEMORY`, `TMPDIR` | every compute-heavy stage |
| `REGIONS` or chromosome list | large genomes and cluster splitting |
| `OUTDIR` | all stages |

Sample table expectations:

```text
# short-read paired FASTQ
sample    fq1    fq2

# existing BAM
sample    bam

# long-read FASTQ
sample    fastq    platform

# assembly comparison
reference_assembly    query_assembly
```

## 输出结果

The workflow router should produce a concrete plan, not raw variant files by itself:

- selected canonical skill IDs.
- input contract for each selected stage.
- expected output files and indexes.
- execution order.
- blocked reason if required inputs are missing.
- final `report.md` checklist for executed stages.

Recommended project layout:

```text
outdir/
  00.input_check/
  01.ref/
  02.mapping/
  03.variant_calling/
  04.filter/
  05.summary/
  logs/
  report.md
  reproduce.sh
```

## 常见错误及解决办法

| Problem | What it means | Fix |
|---|---|---|
| User provides FASTQ but asks for VCF immediately | mapping stage missing | select mapping before calling |
| "WGS" request but files are RNA-seq | wrong evidence model | route to RNA-seq variant calling or ask |
| BAM/VCF contigs differ from reference | incompatible reference build | stop before calling or re-align |
| User asks for "all variants" | ambiguous class | separate SNP/InDel, SV, CNV, and assembly variants |
| Existing outputs present | overwrite risk | create new dated subdir or ask before overwrite |
| Missing tool | dependency blocker | report exact missing command and affected stage |

Verify each actually required scientific output and index against its format contract;
logs or exit status alone do not prove scientific success. A valid zero-record
result is distinct from a corrupt or unexecuted result. Do not impose a nonempty
file requirement on an observation-only input check.

An input-only check may report a bounded observation without creating result
files. Reporting should consume those observations plus actual available results;
never invent a check file or missing VCF/statistics. Retain report.md, reproduce.sh
and a stage-status table when requested. A blocker report records unexecuted stages
and the exact cause; it is not scientific completion.

## 相似的 skills

- `genomics/wgs-short-read-mapping` - map paired-end WGS FASTQ to BAM.
- `genomics/wgs-gatk-snp-indel` - GATK GVCF SNP/InDel calling.
- `genomics/wgs-variant-calling` - existing BioMind robust GATK script renderer for large crop/non-model genomes.
- `genomics/wgs-bcftools-snp-indel` - fast lightweight SNP/InDel calling.
- `genomics/wgs-manta-sv` - default short-read paired-end SV/indel calling.
- `genomics/wgs-lumpy-sv` - legacy short-read split-read/discordant-pair SV fallback.
- `genomics/wgs-cnvnator-cnv` - read-depth CNV.
- `genomics/rnaseq-variant-calling` - RNA-seq variant calling.
- `genomics/long-read-sv-sniffles2` - default ONT/PacBio long-read SV.
- `genomics/long-read-sv-cutesv` - optional long-read SV cross-caller.
- `genomics/pacbio-sv-pbsv` - optional PacBio pbsv workflow.
- `genomics/assembly-variant-syri` - assembly-vs-assembly variants.
- `genomics/assembly-variant-assemblytics` - optional assembly-vs-reference delta SV summary.
- `genomics/assembly-variant-svim-asm` - optional assembly-alignment SV caller.
- `genomics/snpeff-annotation` - annotate final VCF.
