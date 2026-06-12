---
name: crop-rnaseq-pipeline
description: Build crop bulk RNA-seq count and TPM matrices from FASTQ or BAM inputs using nf-core/rnaseq-style workflows. Use for crop transcriptome upstream QC, alignment, quantification, MultiQC, and handoff to crop-rnaseq-de; do not use when the user already has a count/TPM matrix or only wants DEG plotting.
---

# Crop RNA-seq Pipeline

Use this skill for crop bulk RNA-seq upstream processing:

```text
FASTQ/BAM -> QC -> alignment/quantification -> gene counts/TPM -> MultiQC -> crop-rnaseq-de handoff
```

## Trigger Conditions

- User asks for RNA-seq FASTQ to count matrix, crop transcriptome processing, expression matrix construction, STAR/Salmon, HISAT2, nf-core/rnaseq, or batch RNA-seq QC/quantification.
- Input is FASTQ/BAM plus a crop genome FASTA and GFF/GTF annotation.

Do not use for:

- Existing count matrix + metadata DEG analysis: use `crop-rnaseq-de`.
- Existing DE table visualization: use `crop-diff-visualizer`.
- No-replicate TPM fold-change screening: use `crop-rnaseq-de`.

## Inputs

Required:

- `samplesheet.csv` with at least `sample,fastq_1,fastq_2,tissue,genotype,condition,replicate,strandedness`
- `genome_fasta`
- `annotation_gff_or_gtf`
- `output_dir`

Optional:

- `aligner`: `star_salmon`, `star_rsem`, `hisat2`, or `salmon`
- `strandedness`: `auto`, `forward`, `reverse`, or `unstranded`
- `threads`, `genome_index`, `species_name`

## Crop-Specific Rules

- Never default to a human reference genome.
- Require user-provided `genome_fasta` and `annotation_gff_or_gtf` unless an explicit crop reference/index is provided.
- Preserve `gene_id`, `transcript_id`, and `gene_name` mapping when parsing outputs.
- Warn for polyploid or fragmented crop genomes; do not assume one gene ID maps to one biological locus.

## Workflow

1. Validate the samplesheet:
   `python scripts/build_samplesheet.py --input samplesheet.csv --output validated.samplesheet.csv --check-files`
2. Dry-run the nf-core command:
   `bash scripts/run_nfcore_rnaseq.sh --samplesheet validated.samplesheet.csv --genome-fasta genome.fa --annotation genome.gff3 --outdir results/rnaseq_pipeline --dry-run`
3. Run the workflow after reviewing the command.
4. Parse outputs:
   `python scripts/parse_nfcore_outputs.py --nfcore-dir results/rnaseq_pipeline --outdir results/rnaseq_pipeline/parsed`
5. Use `handoff/crop_rnaseq_de.command.sh` for DEG analysis.

## Outputs

Every output directory should contain:

- `report.md`
- `result.json`
- `counts/gene_counts.raw.tsv`
- `counts/gene_counts.length_scaled.tsv`
- `counts/gene_tpm.tsv`
- `counts/transcript_tpm.tsv`
- `qc/multiqc_report.html`
- `qc/sample_mapping_rate.tsv`
- `handoff/crop_rnaseq_de.command.sh`
- `handoff/sample_metadata.template.tsv`
- `handoff/count_matrix_for_de.tsv`
- `reproducibility/commands.sh`, `params.yaml`, `checksums.sha256`

## Gotchas

- Chromosome names in FASTA, GFF/GTF, and BAM/quantification outputs must match.
- For autopolyploid crops, expression assigned to highly similar homeologs may be ambiguous.
- For no biological replicates, do not try to infer statistical DEG here; hand off TPM to `crop-rnaseq-de`.

## Tests

Validate samplesheet parsing and command generation with independent validation data or user-provided sample metadata; do not keep local example samplesheets inside this skill.
