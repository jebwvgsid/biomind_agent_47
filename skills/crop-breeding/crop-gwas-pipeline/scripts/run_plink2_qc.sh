#!/usr/bin/env bash
set -euo pipefail
usage(){ echo "Usage: run_plink2_qc.sh --pfile prefix --phenotype phenotype.tsv --out outdir [--geno 0.1 --mind 0.1 --maf 0.05 --hwe 1e-6 --max-alleles 2 --ld-prune '50 5 0.2']"; }
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
crop_dir="$(cd -- "${script_dir}/../.." && pwd)"
pfile=""; phenotype=""; out=""; geno=0.1; mind=0.1; maf=0.05; hwe=1e-6; max_alleles=2; ld_prune=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pfile) pfile="$2"; shift 2;;
    --phenotype) phenotype="$2"; shift 2;;
    --out) out="$2"; shift 2;;
    --geno) geno="$2"; shift 2;;
    --mind) mind="$2"; shift 2;;
    --maf) maf="$2"; shift 2;;
    --hwe) hwe="$2"; shift 2;;
    --max-alleles) max_alleles="$2"; shift 2;;
    --ld-prune) ld_prune="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown: $1" >&2; usage; exit 2;;
  esac
done
[[ -n "$pfile" && -n "$phenotype" && -n "$out" ]] || { usage >&2; exit 2; }
cmd=(python "${crop_dir}/crop-gwas-qc-plink2-vcftools/scripts/run_crop_gwas_qc.py" --pfile "$pfile" --phenotype "$phenotype" --outdir "$out" --geno "$geno" --mind "$mind" --maf "$maf" --hwe "$hwe" --max-alleles "$max_alleles")
[[ -n "$ld_prune" ]] && cmd+=(--ld-prune "$ld_prune")
exec "${cmd[@]}"
