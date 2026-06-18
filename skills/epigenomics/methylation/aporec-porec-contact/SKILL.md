---
name: aporec-porec-contact
description: >
  Generate AporeC/Pore-C contact-analysis outputs from long-read BAM files:
  read-order/concatemer QC, pairs files, merged pairs, and cooler matrices.
  Use when the user has lab AporeC data, ONT Pore-C data, or a porec.smk-style
  workflow and asks for `.pairs.gz`, `.porec.gz`, concatemer/high-order
  statistics, `.cool` matrices, or 3D genome handoff. Do not use for ordinary
  ONT WGS methylation or second-generation methylation data.
metadata:
  version: "1.0.0"
  tags: [AporeC, Pore-C, nanopore, 3D-genome, pairs, cooler, cool, concatemer, cphasing-rs, methylation-integration]
  legacy_ids: [aporec-contact, porec-contact, porec-pairs-cool, aporec-pairs-cool, PoreC接触矩阵, AporeC互作矩阵]
  task_types: [aporec_methylation, porec_contact_matrix, chromatin_contact_matrix]
  stage_tags: [aporec_contact_matrix]
  inputs: [aligned_bam, reference_fasta, chromsizes, binsizes, sample_table]
  outputs: [pairs_gz, porec_gz, read_summary, concatemer_summary, high_order_hist, merged_pairs_gz, cool_matrices]
  runtime:
    default_env: bio_porec
    env_runner: bash
    language: bash
    packages: [dorado, samtools, cphasing-rs, cphasing, cooler, pigz]
    capabilities: [porec-bam-to-pairs, concatemer-qc, pairs-merge, pairs-to-cool]
    healthcheck: bash -lc "samtools --version >/dev/null && cphasing-rs --help >/dev/null && cphasing --help >/dev/null"
---

# AporeC / Pore-C Contact Matrix

Use this skill for the contact side of AporeC/Pore-C projects. It complements
modkit methylation calling: the same long-read assay can provide modified-base
tracks and multi-way contact information, but ordinary ONT WGS methylation data
does not provide valid Pore-C concatemer contacts.

The commands below mirror the local Snakemake file
`/public/home/liusiqi/02.software/script/porec.smk`.

## Required Inputs

- `REFERENCE_FASTA`: genome FASTA.
- `BAM`: AporeC/Pore-C BAM for each sample. It may be unaligned or aligned,
  but the workflow creates a name-sorted primary-alignment BAM before pairs.
- `SAMPLE`: sample name.
- `BINSIZES`: one or more matrix resolutions.
- `OUTDIR`: output root, usually `results`.
- `THREADS`: default 8 or higher for large libraries.

Do not run this skill when the assay is WGBS, EM-seq, RRBS, ordinary ONT WGS,
PacBio WGS, RNA-seq, or any non-proximity long-read dataset.

## Standard Workflow

Create chromosome sizes:

```bash
cphasing-rs chromsizes "${REFERENCE_FASTA}" > "${OUTDIR}/genome.chromsizes"
```

Align or re-align BAM while keeping primary alignments and read order:

```bash
dorado aligner -t "${THREADS:-8}" \
  --mm2-opts '--secondary=no -Y' \
  "${REFERENCE_FASTA}" "${BAM}" \
  | modtools sup2pri - \
  | samtools sort -T "${OUTDIR}/" -n -@ "${THREADS:-8}" \
  > "${OUTDIR}/${SAMPLE}.nsorted.bam"
```

Collect BAM-level statistics across raw input BAMs:

```bash
cphasing-rs bamstat data/*.bam > "${OUTDIR}/bam.stat"
```

Convert name-sorted BAM to Pore-C pairs outputs:

```bash
cphasing-rs bam2paf "${OUTDIR}/${SAMPLE}.nsorted.bam" \
  | cphasing-rs paf2pairs - "${OUTDIR}/genome.chromsizes" \
      -o "${OUTDIR}/${SAMPLE}.pairs.gz"
```

Expected side outputs from `paf2pairs` in the local workflow are:

```text
${OUTDIR}/${SAMPLE}.porec.gz
${OUTDIR}/${SAMPLE}.porec.read.summary
${OUTDIR}/${SAMPLE}.pairs.concatemer.summary
```

Merge sample-level pairs only after all samples use the same reference:

```bash
cphasing-rs pairs-merge "${OUTDIR}"/*.pairs.gz \
  -o "${OUTDIR}/merged.pairs.gz"
```

Create cooler matrices for each resolution:

```bash
cphasing pairs2cool \
  "${OUTDIR}/merged.pairs.gz" \
  "${OUTDIR}/genome.chromsizes" \
  "${OUTDIR}/merged_${BINSIZE}.cool" \
  --binsize "${BINSIZE}"
```

## Concatemer QC

Generate high-order distribution summaries from per-sample concatemer files:

```bash
plot_high_order_distribution_mean.py \
  -s "${OUTDIR}"/*.pairs.concatemer.summary \
  -o "${OUTDIR}/high.order.distribution.png"
```

If the helper also writes `high.order.distribution.hist`, use:

```bash
plot_high_order_distribution_multiple_group.py \
  -f "${OUTDIR}/high.order.distribution.hist" \
  -o "${OUTDIR}/high.order.distribution.each.png"
```

Report concatemer-order distribution before downstream TAD, compartment, or
loop interpretation. Low high-order signal suggests the library may not support
the intended Pore-C analysis even if methylation calling succeeds.

## Handoff

Primary outputs for downstream skills:

| Output | Next use |
|---|---|
| `merged.pairs.gz` | contact matrix regeneration or pair-level QC |
| `merged_<binsize>.cool` | TAD, compartment, loop, or contact-map analysis |
| `high.order.distribution.png` | library QC report |
| `bam.stat` | input read/alignment summary |

After `.cool` matrices are produced, route 3D genome analysis to
`3d-dna/3d-dna-analysis` and its TAD, compartment, or loop subskills.

## Common Failure Modes

| Symptom | Meaning | Fix |
|---|---|---|
| Empty or tiny `.pairs.gz` | BAM is not Pore-C/AporeC or alignment failed | confirm assay and inspect BAM header/read structure |
| Missing side outputs | `paf2pairs` version or output prefix mismatch | inspect command log and adjust expected filenames |
| `pairs2cool` fails | chromosome sizes differ from pairs contigs | regenerate chromsizes from the alignment reference |
| High-order plot is flat | low concatemer complexity | report as library QC concern before biological claims |
| User provides WGBS/EM-seq | not a contact assay | route to second-generation methylation workflow |
