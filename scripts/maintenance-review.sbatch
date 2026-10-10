#!/bin/bash
# Read-only host evidence in a separate allocation; never attach to a user's job.
# Scheduler target and resources belong to the submission command, not this file.
set -euo pipefail
: "${SLURM_JOB_ID:?Submit this read-only collector with sbatch}"
biomind_review_python=${1:?Absolute Python executable required}
biomind_review_plan=${2:?Private review plan required}
biomind_review_evidence=${3:?Evidence directory required}
biomind_review_scripts=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
test -f "$biomind_review_scripts/../TOOLS.json"
exec "$biomind_review_python" -B "$biomind_review_scripts/collect-maintenance-review.py" \
  --plan "$biomind_review_plan" --output-directory "$biomind_review_evidence"
