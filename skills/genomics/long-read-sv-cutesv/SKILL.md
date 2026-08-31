---
name: long-read-sv-cutesv
description: Optional long-read structural variant calling with cuteSV for ONT, PacBio CLR, or PacBio HiFi BAMs. Use for multi-caller validation, benchmarking, or Sniffles2 failure follow-up, not as the default long-read SV route.
version: "1.0.0"
tags: [long-read, ONT, PacBio, HiFi, SV, cuteSV, optional-caller]
legacy_ids: [cutesv, cuteSV, longread-cutesv]
task_types: [long_read_sv_calling]
stage_tags: [cutesv_sv, optional_long_read_sv]
inputs: [reference_fasta, sorted_bam, platform]
outputs: [cutesv_vcf, sv_summary]
when_to_use:
  - User explicitly requests cuteSV.
  - User requests multi-caller validation or benchmarking for long-read SV.
  - Sniffles2 is unavailable or failed and the user accepts an alternate caller.
when_not_to_use:
  - Vague long-read SV requests; use genomics/long-read-sv-sniffles2 first.
  - Short-read WGS; use genomics/wgs-manta-sv.
runtime:
  default_env: bio_cutesv
  env_runner: bash
  language: bash
  packages: [cutesv, samtools, bcftools]
  capabilities: [long-read-sv, cutesv-calling]
  healthcheck: bash -lc "cuteSV --help >/dev/null 2>&1 && samtools --version >/dev/null && bcftools --version >/dev/null"
  install_command: mamba create -n bio_cutesv -c conda-forge -c bioconda -y cutesv samtools bcftools
---

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
