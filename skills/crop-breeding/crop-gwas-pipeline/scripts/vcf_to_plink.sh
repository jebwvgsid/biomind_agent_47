#!/usr/bin/env bash
set -euo pipefail
usage(){ echo "Usage: vcf_to_plink.sh --vcf input.vcf.gz --phenotype phenotype.tsv --out outdir [--max-alleles 2]"; }
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../../../.." && pwd)"
vcf=""; phenotype=""; out=""; max_alleles=2
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vcf) vcf="$2"; shift 2;;
    --phenotype) phenotype="$2"; shift 2;;
    --out) out="$2"; shift 2;;
    --max-alleles) max_alleles="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown: $1" >&2; usage; exit 2;;
  esac
done
[[ -n "$vcf" && -n "$phenotype" && -n "$out" ]] || { usage >&2; exit 2; }
exec python "${repo_root}/skills/crop-breeding/crop-gwas-qc-plink2-vcftools/scripts/run_crop_gwas_qc.py" \
  --vcf "$vcf" \
  --phenotype "$phenotype" \
  --outdir "$out" \
  --max-alleles "$max_alleles"
