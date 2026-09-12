---
name: reseq-variant-workflow
description: Route resequencing variant analysis across short-read WGS SNP/InDel, short-read SV, read-depth CNV, RNA-seq variant calling, long-read SV, and assembly-vs-assembly SyRI workflows. Use when the user asks for 重测序变异检测, SNP, InDel, SV, CNV, RNA-seq variants, long-read structural variants, or assembly-based variants and BioMind must choose the right primary downstream skill instead of running every method.
version: "1.0.0"
tags: [reseq, resequencing, variant-calling, SNP, InDel, SV, CNV, RNA-seq, long-read, SyRI, workflow-router]
legacy_ids: [reseq_variant_calling, resequencing-variant-workflow, 重测序变异检测]
task_types: [variant_calling_workflow]
stage_tags: [workflow_routing, input_triage]
inputs: [reference_fasta, sample_table, fastq, bam, assembly_fasta, analysis_goal]
outputs: [stage_plan, selected_skills, output_contract, blocked_reason]
---

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

In the approved Plan, 00.input_check may deliver a scoped observation with
outputs=[] through an existing inspect capability. 05.summary may use an existing
file-producing capability with descriptive purpose=validation. 06.reporting must
consume accepted predecessor observations plus actual available scientific results
and stage facts; it must not invent an intermediate check file. Retain report.md,
reproduce.sh and the stage status table when the user requests them. A descriptive
record_failure purpose cannot decide terminal status. If real dependency evidence
requires stopping, use the existing authorized stop/report path to record the
unexecuted stages and exact blocker; producing that report is not scientific
success and does not create missing VCF or statistics.

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
