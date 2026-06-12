#!/usr/bin/env bash
set -euo pipefail
usage(){ echo "Usage: normalize_filter_vcf.sh --vcf input.vcf.gz --reference genome.fa --out output.vcf.gz [--min-qual 20]"; }
vcf=""; ref=""; out=""; min_qual=20
while [[ $# -gt 0 ]]; do
  case "$1" in --vcf) vcf="$2"; shift 2;; --reference) ref="$2"; shift 2;; --out) out="$2"; shift 2;; --min-qual) min_qual="$2"; shift 2;; -h|--help) usage; exit 0;; *) echo "Unknown: $1" >&2; usage; exit 2;; esac
done
[[ -n "$vcf" && -n "$ref" && -n "$out" ]] || { usage >&2; exit 2; }
mkdir -p "$(dirname "$out")"
bcftools norm -f "$ref" -m -any "$vcf" -Oz -o "$out"
bcftools filter -i "QUAL>=${min_qual} || QUAL='.'" "$out" -Oz -o "${out%.vcf.gz}.filtered.vcf.gz"
bcftools index -t "${out%.vcf.gz}.filtered.vcf.gz"
echo "${out%.vcf.gz}.filtered.vcf.gz"
