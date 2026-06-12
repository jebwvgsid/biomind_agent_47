#!/usr/bin/env bash
set -euo pipefail
usage(){ echo "Usage: call_variants_or_accept_vcf.sh --input-vcf samples.vcf.gz --out samples.vcf.gz"; }
input_vcf=""; out=""
while [[ $# -gt 0 ]]; do
  case "$1" in --input-vcf) input_vcf="$2"; shift 2;; --out) out="$2"; shift 2;; -h|--help) usage; exit 0;; *) echo "Unknown: $1" >&2; usage; exit 2;; esac
done
[[ -n "$input_vcf" && -n "$out" ]] || { usage >&2; exit 2; }
mkdir -p "$(dirname "$out")"
cp "$input_vcf" "$out"
echo "Accepted existing VCF: $out"
