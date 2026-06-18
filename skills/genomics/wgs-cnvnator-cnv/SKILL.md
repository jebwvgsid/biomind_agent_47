---
name: wgs-cnvnator-cnv
description: Detect read-depth copy-number variants from short-read WGS BAMs with CNVnator, including bin-size selection, histogram/stat/eval/partition/call steps, optional VCF conversion, filtering, and CNV summaries. Use for deletion/duplication CNV screening from resequencing BAMs. Do not use for RNA-seq, long-read SV, or split-read SV-only analysis.
version: "1.0.0"
tags: [WGS, CNV, CNVnator, read-depth, deletion, duplication, copy-number]
legacy_ids: [cnvnator-cnv, wgs-cnv, read-depth-cnv]
task_types: [wgs_cnv_calling]
stage_tags: [cnvnator, read_depth_cnv]
inputs: [reference_fasta, sorted_bam, bin_size]
outputs: [cnvnator_root, cnv_calls, filtered_cnv_table, cnv_vcf, eval_report]
runtime:
  default_env: bio_cnvnator
  env_runner: bash
  language: bash
  packages: [cnvnator, root, samtools, seqkit]
  capabilities: [read-depth-cnv, cnv-filtering]
  healthcheck: bash -lc "cnvnator -h >/dev/null 2>&1 && samtools --version >/dev/null"
---

# WGS CNVnator CNV Calling

Use this skill for read-depth based CNV detection from short-read WGS BAMs. CNVnator is per-sample by design; preserve sample identity when summarizing.

## 确认任务边界

Use this skill when:

- Input is WGS BAM aligned to the same reference.
- The biological goal is copy-number deletions/duplications from read depth.
- The user accepts resolution limits determined by depth and bin size.

Do not use this skill when:

- Input is RNA-seq.
- The goal is split-read/discordant-pair SV; use LUMPY.
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

- `genomics/wgs-lumpy-sv` - short-read split-read/discordant-pair SV.
- `genomics/long-read-sv-sniffles-survivor` - long-read SV.
- `genomics/wgs-short-read-mapping` - prepare BAMs.
- `genomics/assembly-variant-syri` - assembly-level structural variation.
