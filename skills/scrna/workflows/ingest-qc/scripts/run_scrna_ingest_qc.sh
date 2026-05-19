#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' \
"Usage: bash skills/scrna/workflows/ingest-qc/scripts/run_scrna_ingest_qc.sh <manifest.yaml> [conda_env]" \
"" \
"Runs:" \
"  1. skills/scrna/env/bootstrap/scripts/check_scrna_env.R" \
"  2. skills/scrna/ingest/data-download/scripts/ingest_scrna_seurat.R --manifest <manifest.yaml>" \
"  3. skills/scrna/qc/filter/scripts/qc_filter_seurat.R --manifest <manifest.yaml>" \
"" \
"Default conda_env: bio_scrna_core"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 1
fi

MANIFEST="$1"
CONDA_ENV="${2:-bio_scrna_core}"

if [[ ! -f "${MANIFEST}" ]]; then
  echo "Manifest not found: ${MANIFEST}" >&2
  exit 1
fi

echo "[1/3] Checking scRNA environment: ${CONDA_ENV}"
conda run -n "${CONDA_ENV}" Rscript skills/scrna/env/bootstrap/scripts/check_scrna_env.R

echo "[2/3] Building Seurat object from manifest: ${MANIFEST}"
conda run -n "${CONDA_ENV}" Rscript skills/scrna/ingest/data-download/scripts/ingest_scrna_seurat.R --manifest "${MANIFEST}"

echo "[3/3] Running QC filter from manifest: ${MANIFEST}"
conda run -n "${CONDA_ENV}" Rscript skills/scrna/qc/filter/scripts/qc_filter_seurat.R --manifest "${MANIFEST}"

echo "Done: ingest + QC completed for ${MANIFEST}"
