---
name: assembly-variant-assemblytics
description: Optional assembly-vs-reference structural variant summarization with Assemblytics from MUMmer delta alignments. Use as supplemental assembly-level SV analysis when the user explicitly requests Assemblytics or has a suitable delta file.
version: "1.0.0"
tags: [assembly, Assemblytics, MUMmer, delta, structural-variant, optional-caller]
legacy_ids: [assemblytics, assemblytics-sv]
task_types: [assembly_variant_calling]
stage_tags: [assemblytics_sv, optional_assembly_sv]
inputs: [delta_alignment, reference_assembly_fasta, query_assembly_fasta]
outputs: [assemblytics_table, assemblytics_bed, assemblytics_summary]
when_to_use:
  - User explicitly requests Assemblytics.
  - A formal MUMmer delta alignment exists and supplemental assembly SV summary is requested.
when_not_to_use:
  - Default assembly-level SV requests; use genomics/assembly-variant-syri first.
  - FASTQ/BAM read-based SV.
runtime:
  default_env: bio_assemblytics
  env_runner: bash
  language: bash
  packages: [assemblytics, mummer]
  capabilities: [assembly-delta-sv, assemblytics-summary]
  healthcheck: bash -lc "Assemblytics --help >/dev/null 2>&1 || assemblytics --help >/dev/null 2>&1"
  install_command: mamba create -n bio_assemblytics -c conda-forge -c bioconda -y assemblytics mummer
---

# Optional Assembly SV with Assemblytics

Use Assemblytics only as supplemental assembly-level analysis or when the user explicitly requests it. The default assembly-level workflow is `genomics/assembly-variant-syri`.

## Execution Guidance

Input is a MUMmer delta file from a reference-vs-query assembly alignment. If no delta exists, generate it only when both assemblies are provided and the plan includes this optional stage.

Check the installed command spelling before use because packages may expose `Assemblytics` or `assemblytics`.

```bash
nucmer --maxmatch -p "${OUTDIR}/03.variant_calling/out" \
  "${REFERENCE_ASSEMBLY}" "${QUERY_ASSEMBLY}"
delta-filter -m "${OUTDIR}/03.variant_calling/out.delta" \
  > "${OUTDIR}/03.variant_calling/out.filtered.delta"
Assemblytics "${OUTDIR}/03.variant_calling/out.filtered.delta" \
  "${OUTDIR}/04.filter/assemblytics" \
  "${UNIQUE_ANCHOR_LENGTH:-10000}" "${MIN_VARIANT_SIZE:-50}" "${MAX_VARIANT_SIZE:-10000000}"
```

## Outputs and Validation

Validate non-empty Assemblytics result tables/BED files and summarize SV classes. If the command produces no variants, distinguish valid no-call output from failed/empty files.
