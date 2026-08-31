---
name: wgs-manta-sv
description: Primary short-read paired-end WGS structural variant and indel calling with Manta from coordinate-sorted indexed BAM or CRAM files. Use as the default short-read SV skill instead of LUMPY unless the user explicitly requests LUMPY or legacy split-read evidence.
version: "1.0.0"
tags: [WGS, short-read, paired-end, SV, Manta, deletion, duplication, inversion, translocation, indel, primary-caller]
legacy_ids: [manta-sv, short-read-manta, wgs-sv-manta, 短读长SV, 短读长结构变异]
task_types: [wgs_structural_variant_calling]
stage_tags: [manta_sv, short_read_sv]
inputs: [reference_fasta, sorted_bam, sorted_cram, bam_list]
outputs: [manta_run_dir, diploid_sv_vcf, candidate_sv_vcf, sv_summary]
when_to_use:
  - Illumina/MGI paired-end WGS BAM/CRAM structural variant calling.
  - Short-read SV requests where no caller is specified.
  - Germline or somatic Manta workflows when the sample design is clear.
when_not_to_use:
  - ONT/PacBio long reads; use genomics/long-read-sv-sniffles2.
  - Read-depth CNV-only analysis; use genomics/wgs-cnvnator-cnv.
  - RNA-seq variant calling unless the user explicitly requests Manta RNA mode.
runtime:
  default_env: bio_manta
  env_runner: bash
  language: bash
  packages: [manta, samtools, bcftools]
  capabilities: [short-read-sv, manta-config, manta-workflow]
  healthcheck: bash -lc "(configManta.py --help >/dev/null 2>&1 || configureManta.py --help >/dev/null 2>&1) && samtools --version >/dev/null && bcftools --version >/dev/null"
  install_command: mamba create -n bio_manta -c conda-forge -c bioconda -y manta samtools bcftools
---

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
