---
name: modkit-methylation-pileup
description: >
  Run Oxford Nanopore modified-base methylation pileup from aligned modBAM files
  with MM/ML tags, then optionally compare site-level 5mC methylation ratios
  against bisulfite, EM-seq, or bedMethyl truth tables. Use when the user asks
  for ONT 5mC/5hmC methylation detection, modkit pileup, modBAM tag checks,
  bedMethyl generation, or methylation benchmark metrics such as overlap,
  Pearson/Spearman correlation, RMSE, precision, and recall. Do not use for
  6mA open-chromatin prediction; use open-chromatin/modkit-cpu for that task.
version: "1.0.0"
tags: [nanopore, methylation, modkit, modBAM, MM, ML, 5mC, 5hmC, bedMethyl, benchmark]
task_types: [methylation_nanopore]
stage_tags: [modkit_pileup, methylation_truth_comparison]
inputs: [modbam_bam, reference_fasta, truth_bedmethyl, bisulfite_truth_table]
outputs: [modkit_pileup_bed, methylation_comparison_table, metrics_json]
when_to_use:
  - User provides an aligned ONT modBAM with MM/ML modified-base tags.
  - User asks to run modkit pileup for 5mC or compare ONT methylation to bisulfite/EM-seq truth.
when_not_to_use:
  - The task is 6mA open-chromatin prediction from modBAM accessibility signal.
  - The input is FASTQ only and no POD5/FAST5 or modBAM is available.
---

# Modkit Methylation Pileup

Use this skill for ONT modified-base methylation analysis from aligned modBAM
files. It is a methylation-calling and benchmark skill, not an open-chromatin
prediction skill.

## Required Inputs

- Aligned `modbam.bam` containing `MM` and `ML` tags.
- `reference.fa` matching the BAM alignment reference.
- Optional truth table such as bisulfite, EM-seq, or bedMethyl for benchmark
  comparison.

## Core Workflow

1. Check that the BAM is indexed or create an index when allowed.
2. Run `modkit modbam check-tags` and stop if modified-base tags are missing.
3. Run `modkit pileup` with an explicit reference and requested modified base.
4. If truth is provided, intersect callable sites after coverage filtering.
5. Report site counts, overlap counts, methylation-ratio correlation, RMSE or
   MAE, and any reason the benchmark cannot be scored.

## Expected Outputs

- `modkit_pileup.bed.gz`
- `methylation_comparison.tsv`
- `metrics.json`

## Safety And Interpretation

- Do not claim methylation detection from plain FASTQ alone.
- Do not mix 5mC methylation benchmarking with 6mA open-chromatin prediction.
- Do not report success if `modkit pileup` is empty or tag checks fail.
