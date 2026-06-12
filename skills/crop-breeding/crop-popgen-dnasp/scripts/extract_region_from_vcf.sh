#!/usr/bin/env bash
set -euo pipefail
usage(){ echo "Usage: extract_region_from_vcf.sh --vcf population.vcf.gz --regions candidate_regions.bed --outdir out"; }
vcf=""; regions=""; outdir=""
while [[ $# -gt 0 ]]; do
  case "$1" in --vcf) vcf="$2"; shift 2;; --regions|--region-bed) regions="$2"; shift 2;; --outdir) outdir="$2"; shift 2;; -h|--help) usage; exit 0;; *) echo "Unknown: $1" >&2; usage; exit 2;; esac
done
[[ -n "$vcf" && -n "$regions" && -n "$outdir" ]] || { usage >&2; exit 2; }
mkdir -p "$outdir"
bcftools view -R "$regions" "$vcf" -Oz -o "$outdir/regions.vcf.gz"
bcftools index -t "$outdir/regions.vcf.gz"
