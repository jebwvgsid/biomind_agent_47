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
"Without conda_env, use Rscript from the current environment"
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
CONDA_ENV="${2:-}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCRNA_DIR="$(cd -- "${SCRIPT_DIR}/../../.." && pwd)"
runner=(Rscript)
if [[ -n "${CONDA_ENV}" ]]; then runner=(conda run -n "${CONDA_ENV}" Rscript); fi

if [[ ! -f "${MANIFEST}" ]]; then
  echo "Manifest not found: ${MANIFEST}" >&2
  exit 1
fi

echo "[1/3] Checking scRNA environment: ${CONDA_ENV}"
"${runner[@]}" "${SCRNA_DIR}/env/bootstrap/scripts/check_scrna_env.R"

echo "[2/3] Building Seurat object from manifest: ${MANIFEST}"
"${runner[@]}" "${SCRNA_DIR}/ingest/data-download/scripts/ingest_scrna_seurat.R" --manifest "${MANIFEST}"

echo "[3/3] Running QC filter from manifest: ${MANIFEST}"
"${runner[@]}" "${SCRNA_DIR}/qc/filter/scripts/qc_filter_seurat.R" --manifest "${MANIFEST}"

echo "Done: ingest + QC completed for ${MANIFEST}"
