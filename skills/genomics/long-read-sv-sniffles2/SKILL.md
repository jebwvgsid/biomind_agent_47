---
name: long-read-sv-sniffles2
description: Primary long-read structural variant calling with Sniffles2 from ONT, PacBio CLR, or PacBio HiFi FASTQ/BAM. Use as the default long-read SV skill unless the user explicitly asks for another caller or multi-caller validation.
version: "1.0.0"
tags: [long-read, ONT, PacBio, HiFi, SV, Sniffles2, minimap2, structural-variant, primary-caller]
legacy_ids: [sniffles2-sv, sniffles2, longread-sniffles2, nanopore-sniffles2, pacbio-sniffles2]
task_types: [long_read_sv_calling]
stage_tags: [long_read_mapping, sniffles2_sv]
inputs: [reference_fasta, long_read_fastq, long_read_bam, platform]
outputs: [long_read_bam, sniffles2_vcf, snf_file, sv_summary]
when_to_use:
  - ONT or PacBio FASTQ/BAM with a request for structural variant discovery.
  - Vague long-read SV requests where one primary caller should be used.
  - Sniffles2 SNF cohort mode or force genotyping when explicitly requested.
when_not_to_use:
  - Short-read paired-end WGS; use genomics/wgs-manta-sv.
  - Read-depth CNV-only analysis; use genomics/wgs-cnvnator-cnv.
  - Assembly-vs-assembly comparison; use genomics/assembly-variant-syri.
runtime:
  default_env: bio_longread_sv
  env_runner: bash
  language: bash
  packages: [minimap2, samtools, sniffles, bcftools]
  capabilities: [long-read-alignment, sniffles2-sv-calling, snf-genotyping]
  healthcheck: bash -lc "minimap2 --version >/dev/null && samtools --version >/dev/null && sniffles --version >/dev/null && sniffles --help 2>&1 | grep -E -- '--input|--vcf' >/dev/null"
  install_command: mamba create -n bio_longread_sv -c conda-forge -c bioconda -y minimap2 samtools sniffles bcftools
---

# Long-Read SV with Sniffles2

This is the default primary long-read SV skill. Use one Sniffles2 workflow first; do not add cuteSV or pbsv unless the user requests multi-caller validation, PacBio-specific pbsv analysis, or Sniffles2 fails.

## Task Boundary

Use this skill when:

- Input is ONT, PacBio CLR, or PacBio HiFi FASTQ/BAM.
- The goal is DEL, INS, DUP, INV, BND/TRA, or general long-read SV discovery.
- The user wants a single default long-read SV caller.

Do not use this skill for short-read Manta/LUMPY SV, CNVnator CNV, RNA-seq variants, or assembly-level SV.

If only FASTQ is provided, first align with minimap2 using the correct preset:

| Platform | Preset |
|---|---|
| ONT | `map-ont` |
| PacBio CLR | `map-pb` |
| PacBio HiFi | `map-hifi` |

Ask the user only if the platform cannot be inferred from file names, metadata, or task text.

## Execution Guidance

Preflight:

```bash
sniffles --version
sniffles --help
samtools faidx "${REFERENCE_FASTA}"
```

FASTQ to BAM:

```bash
minimap2 -x "${PRESET}" -t "${THREADS:-16}" --MD --cs -a \
  "${REFERENCE_FASTA}" "${FASTQ}" |
samtools sort -@ "${THREADS:-16}" -o "${OUTDIR}/02.mapping/${sample}.sort.bam" -
samtools index "${OUTDIR}/02.mapping/${sample}.sort.bam"
```

Single-sample Sniffles2:

```bash
sniffles \
  --input "${BAM}" \
  --vcf "${OUTDIR}/03.variant_calling/${sample}.sniffles2.vcf" \
  --threads "${THREADS:-8}" \
  --minsvlen "${MIN_SV_LEN:-50}"
```

Optional SNF mode for cohort genotyping only when requested:

```bash
sniffles --input "${BAM}" --snf "${OUTDIR}/03.variant_calling/${sample}.snf" --threads "${THREADS:-8}"
sniffles --input "${OUTDIR}/03.variant_calling/"*.snf --vcf "${OUTDIR}/04.filter/cohort.sniffles2.vcf"
```

## Outputs and Validation

Expected outputs:

- `02.mapping/<sample>.sort.bam` and `.bai` when FASTQ was mapped.
- `03.variant_calling/<sample>.sniffles2.vcf`.
- optional `03.variant_calling/<sample>.snf`.
- `05.summary/sniffles2_sv_counts.tsv`.

Validation:

```bash
test -s "${OUTDIR}/03.variant_calling/${sample}.sniffles2.vcf"
grep -v '^#' "${OUTDIR}/03.variant_calling/${sample}.sniffles2.vcf" |
  awk -F'\t' '{print $8}' | tr ';' '\n' | grep '^SVTYPE=' | sort | uniq -c \
  > "${OUTDIR}/05.summary/sniffles2_sv_counts.tsv"
```

A zero-byte VCF or missing BAM index is not success. An empty but valid VCF may be reported as `no_calls` only after the command completed and the VCF header exists.

## Common Problems

| Problem | Meaning | Fix |
|---|---|---|
| Sniffles reports unknown option | installed Sniffles is not v2 syntax | inspect `sniffles --help`; use this skill only with v2-compatible syntax |
| too few SVs | low depth, high support threshold, poor mapping | inspect depth/read length and tune support |
| mixed references | cohort comparison invalid | re-align to one reference |
| platform unclear | wrong minimap2 preset risk | ask user before mapping |

## Related Skills

- `genomics/long-read-sv-cutesv` - optional cross-caller validation.
- `genomics/pacbio-sv-pbsv` - optional PacBio pbsv workflow.
- `genomics/wgs-manta-sv` - short-read SV.
- `genomics/assembly-variant-syri` - assembly-level SV.
