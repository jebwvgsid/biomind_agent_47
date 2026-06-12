#!/usr/bin/env bash
set -euo pipefail
usage(){ echo "Usage: run_gemma_gwas.sh --bfile plink_prefix --phenotype phenotype.txt --out out_prefix [--trait trait] [--covariates cov.txt]"; }
bfile=""; phenotype=""; out=""; trait=""; covariates=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bfile) bfile="$2"; shift 2;;
    --phenotype) phenotype="$2"; shift 2;;
    --out) out="$2"; shift 2;;
    --trait) trait="$2"; shift 2;;
    --covariates) covariates="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown: $1" >&2; usage; exit 2;;
  esac
done
[[ -n "$bfile" && -n "$phenotype" && -n "$out" ]] || { usage >&2; exit 2; }
[[ -n "$trait" ]] || trait="$(basename "$out")"
outdir="$(dirname "$out")"
cmd=(python skills/crop-breeding/crop-gwas-gemma-lmm/scripts/run_gemma_lmm.py --bfile "$bfile" --phenotype "$phenotype" --trait "$trait" --outdir "$outdir")
[[ -n "$covariates" ]] && cmd+=(--covariates "$covariates")
exec "${cmd[@]}"
