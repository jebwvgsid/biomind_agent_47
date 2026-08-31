---
name: assembly-variant-svim-asm
description: Optional assembly-level structural variant calling with SVIM-asm from minimap2 assembly-to-reference BAM alignments. Use when the user explicitly requests SVIM-asm or SyRI is not suitable for the assembly input.
version: "1.0.0"
tags: [assembly, SVIM-asm, minimap2, BAM, structural-variant, optional-caller]
legacy_ids: [svim-asm, svim_asm, assembly-svim]
task_types: [assembly_variant_calling]
stage_tags: [svim_asm_sv, optional_assembly_sv]
inputs: [reference_assembly_fasta, query_assembly_fasta, assembly_alignment_bam]
outputs: [svim_asm_vcf, sv_summary]
when_to_use:
  - User explicitly requests SVIM-asm.
  - Supplemental assembly-level caller is needed from assembly-to-reference BAM alignment.
  - SyRI chromosome-level assumptions are not appropriate but assembly alignment SV calls are still useful.
when_not_to_use:
  - Default assembly-level SV/synteny requests; use genomics/assembly-variant-syri first.
  - FASTQ/BAM read data from sequencing reads.
runtime:
  default_env: bio_svim_asm
  env_runner: bash
  language: bash
  packages: [svim-asm, minimap2, samtools, bcftools]
  capabilities: [assembly-alignment-sv, svim-asm-calling]
  healthcheck: bash -lc "svim-asm --help >/dev/null 2>&1 && minimap2 --version >/dev/null && samtools --version >/dev/null"
  install_command: mamba create -n bio_svim_asm -c conda-forge -c bioconda -y svim-asm minimap2 samtools bcftools
---

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
