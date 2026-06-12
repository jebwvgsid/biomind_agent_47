#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: run_nfcore_rnaseq.sh --samplesheet samplesheet.csv --genome-fasta genome.fa --annotation genome.gff3 --outdir out [options]

Options:
  --aligner NAME        star_salmon, star_rsem, hisat2, or salmon (default: star_salmon)
  --threads N          Max CPUs (default: 16)
  --genome-index PATH  Optional prebuilt index path
  --species-name NAME  Crop/species label for reports
  --dry-run            Print command and write reproducibility files only
  --nextflow CMD       Nextflow executable (default: nextflow)
EOF
}

samplesheet=""; genome_fasta=""; annotation=""; outdir=""; aligner="star_salmon"; threads="16"; genome_index=""; species_name="custom"; dry_run=0; nextflow_cmd="nextflow"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --samplesheet) samplesheet="$2"; shift 2;;
    --genome-fasta) genome_fasta="$2"; shift 2;;
    --annotation|--annotation-gff-or-gtf) annotation="$2"; shift 2;;
    --outdir) outdir="$2"; shift 2;;
    --aligner) aligner="$2"; shift 2;;
    --threads) threads="$2"; shift 2;;
    --genome-index) genome_index="$2"; shift 2;;
    --species-name) species_name="$2"; shift 2;;
    --nextflow) nextflow_cmd="$2"; shift 2;;
    --dry-run) dry_run=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2;;
  esac
done

[[ -n "$samplesheet" && -n "$genome_fasta" && -n "$annotation" && -n "$outdir" ]] || { usage >&2; exit 2; }
mkdir -p "$outdir/reproducibility"

cmd=("$nextflow_cmd" run nf-core/rnaseq -profile conda
  --input "$samplesheet"
  --fasta "$genome_fasta"
  --gtf "$annotation"
  --aligner "$aligner"
  --outdir "$outdir"
  --max_cpus "$threads")
[[ -n "$genome_index" ]] && cmd+=(--star_index "$genome_index")

printf "%q " "${cmd[@]}" | tee "$outdir/reproducibility/commands.sh"
printf "\n" >> "$outdir/reproducibility/commands.sh"
cat > "$outdir/reproducibility/params.yaml" <<EOF
species_name: "$species_name"
aligner: "$aligner"
threads: $threads
genome_fasta: "$genome_fasta"
annotation_gff_or_gtf: "$annotation"
samplesheet: "$samplesheet"
EOF

if [[ "$dry_run" -eq 1 ]]; then
  echo "[dry-run] command written to $outdir/reproducibility/commands.sh"
  exit 0
fi
"${cmd[@]}"
