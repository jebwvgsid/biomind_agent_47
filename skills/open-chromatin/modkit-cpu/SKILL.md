---
name: modkit-open-chromatin-cpu
description: >
  Run Nanopore modkit open-chromatin prediction on 6mA modBAM files using CPU-only
  execution. Use when the user asks for chromatin accessibility analysis,
  open chromatin prediction, modkit open-chromatin, 6mA accessibility, A+a tag
  checks, small-region CPU tests, include-bed region runs, or chromosome-by-
  chromosome CPU workflows from nanopore modBAM data.
version: "1.0.0"
tags: [chromatin-accessibility, open-chromatin, modkit, nanopore, 6mA, modBAM, A+a, bedGraph, BED, CPU]
legacy_ids: [chromatin_accessibility_cpu, open_chromatin_cpu, modkit_open_chromatin, 染色质可及性分析]
runtime:
  default_env: bio_modkit
  env_runner: bash
  language: bash
  packages: [modkit, samtools, bedtools]
  capabilities: [modkit-open-chromatin, cpu-prediction, modbam-preflight, bedgraph-thresholding]
  healthcheck: bash skills/open-chromatin/modkit-cpu/scripts/run_modkit_open_chromatin_cpu.sh --help
---

# Modkit Open-Chromatin CPU Workflow

## Overview

This skill runs `modkit open-chromatin predict` for nanopore modBAM chromatin accessibility analysis. It is CPU-only and deliberately refuses unscoped whole-genome runs, because CPU prediction can be slow on large genomes.

Primary outputs:

| Output | Description |
|---|---|
| `{OUTDIR}/bedgraph/{SAMPLE}.{scope}.open_chromatin.raw.bedgraph` | Raw accessibility probabilities from modkit |
| `{OUTDIR}/bedgraph/{SAMPLE}.{scope}.open_chromatin.threshold{THRESHOLD}.bedgraph` | Rows with probability >= threshold |
| `{OUTDIR}/bed/{SAMPLE}.{scope}.open_chromatin.threshold{THRESHOLD}.merged.bed` | Merged accessible intervals |
| `{OUTDIR}/logs/{SAMPLE}.modbam_check_tags.txt` | modBAM tag preflight report |
| `{OUTDIR}/logs/{SAMPLE}.open_chromatin_outputs.tsv` | Output manifest |

Reference: Nanopore modkit open-chromatin documentation, https://nanoporetech.github.io/modkit/intro_open_chromatin.html.

## When To Use

Use this skill when:

- User asks for `染色质可及性分析`, `chromatin accessibility`, `open chromatin`, or `modkit open-chromatin`.
- Input is a coordinate-sorted, indexed modBAM with modified-base tags from Dorado.
- The analysis expects the 6mA tag `A+a`.
- User wants CPU execution only.

Do not proceed if `A+a` is absent. Tell the user to return to Dorado basecalling / modified-base calling with a 6mA model.

## Required Tools

- `modkit` executable
- `samtools`
- `bedtools`

Recommended runtime environment: `bio_modkit`.

## Mandatory Preflight

The script always runs these checks in order before prediction:

1. Required variables: `MODKIT`, `MODEL_DIR`, `BAM`, `OUTDIR`, `SAMPLE`
2. File checks: executable `MODKIT`, existing `MODEL_DIR`, non-empty `BAM`
3. BAM integrity and index: `samtools quickcheck -v`, then `.bai` or `.csi`
4. Modified-base tag report: `modkit modbam check-tags --head 1000`
5. Hard stop unless the report contains the expected 6mA tag `A+a`
6. Help inspection: records `open-chromatin predict --help` and enables `--device cpu` only if supported

## CPU Execution Policy

Preferred order:

1. Run a small region test with `REGION=contig:start-end`.
2. Run selected regions with `INCLUDE_BED=/path/to/regions.bed`.
3. Run chromosome-by-chromosome with `CHROMOSOMES="chr1 chr2"` only after a test succeeds.
4. Run whole genome only for small genomes or explicit user request, with `ALLOW_WHOLE_GENOME=1`.

The script refuses to run if none of `REGION`, `INCLUDE_BED`, `CHROMOSOMES`, or `ALLOW_WHOLE_GENOME=1` is set.

## Quick Start: Small Region CPU Test

```bash
MODKIT=/path/to/modkit \
MODEL_DIR=/path/to/open_chromatin_model \
BAM=/path/to/sample.sorted.bam \
OUTDIR=/path/to/open_chromatin_out \
SAMPLE=sample_name \
REGION=Chr01:100000-200000 \
bash skills/open-chromatin/modkit-cpu/scripts/run_modkit_open_chromatin_cpu.sh
```

Optional parameters:

```bash
STEP_SIZE=25
THRESHOLD=0.8
MIN_COVERAGE=5
FORCE=1
```

## Selected Regions

```bash
MODKIT=/path/to/modkit \
MODEL_DIR=/path/to/open_chromatin_model \
BAM=/path/to/sample.sorted.bam \
OUTDIR=/path/to/open_chromatin_out \
SAMPLE=sample_name \
INCLUDE_BED=/path/to/regions.bed \
RUN_LABEL=selected_regions \
bash skills/open-chromatin/modkit-cpu/scripts/run_modkit_open_chromatin_cpu.sh
```

## Chromosome-By-Chromosome

Use this only after a small-region test succeeds:

```bash
MODKIT=/path/to/modkit \
MODEL_DIR=/path/to/open_chromatin_model \
BAM=/path/to/sample.sorted.bam \
OUTDIR=/path/to/open_chromatin_out \
SAMPLE=sample_name \
CHROMOSOMES="Chr01 Chr02 Chr03" \
bash skills/open-chromatin/modkit-cpu/scripts/run_modkit_open_chromatin_cpu.sh
```

## Whole-Genome CPU Run

Only use for small genomes or when the user explicitly requests it:

```bash
MODKIT=/path/to/modkit \
MODEL_DIR=/path/to/open_chromatin_model \
BAM=/path/to/sample.sorted.bam \
OUTDIR=/path/to/open_chromatin_out \
SAMPLE=sample_name \
ALLOW_WHOLE_GENOME=1 \
RUN_LABEL=whole_genome \
bash skills/open-chromatin/modkit-cpu/scripts/run_modkit_open_chromatin_cpu.sh
```

## Troubleshooting

| Error | Meaning | Fix |
|---|---|---|
| `No A+a 6mA tag found` | BAM lacks expected 6mA modified-base calls | Re-run Dorado modified-base calling with a 6mA model |
| `BAM index not found` | `.bai`/`.csi` missing | `samtools index sample.sorted.bam` |
| `REGION was set but ... --region` | Installed modkit does not support `--region` | Use `INCLUDE_BED` |
| `No execution scope set` | CPU run has no range restriction | Start with `REGION` or `INCLUDE_BED` |
| `bedtools not found` | Merged BED generation cannot run | Install/activate bedtools in the runtime environment |

## Notes For Agents

- Never skip the `A+a` check.
- Do not start a large whole-genome CPU job by default.
- `THREADS` is intentionally not used because documented `open-chromatin predict -t` is `--min-coverage`, not worker threads.
- Prefer changing environment variables over editing the script.
