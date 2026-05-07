---
name: TAD Calling with hicFindTADs
description: Runs hicFindTADs from HiCExplorer to identify Topologically Associated Domains (TADs) using a .cool contact matrix.
legacy_ids: [tad_calling]
requirements: ["hicexplorer"]
---
### Overview
Use this skill when the user wants to identify Topologically Associated Domains (TADs) and boundary regions from a 3D genomic contact matrix (e.g., Hi-C or Pore-C data in `.cool` format).

### Execution Template
Output the following bash script within your `<execute>` block. Since TAD calling on high-resolution matrices (e.g., 20k) can be time-consuming, it MUST run in the background.

```bash
#!/bin/bash
# Step 1: Run hicFindTADs in the background
MATRIX_FILE="merged_20k.cool" # Agent: Replace with the actual .cool file path
PREFIX="aPoreC_TADs"          # Agent: Replace with the desired output prefix
THREADS=32

echo "Starting hicFindTADs on $MATRIX_FILE..."
nohup hicFindTADs -m $MATRIX_FILE \
    --outPrefix $PREFIX \
    --numberOfProcessors $THREADS \
    --correctForMultipleTesting fdr > hicFindTADs.log 2>&1 &
    
PID_TADS=$!
echo "hicFindTADs is running in the background with PID: $PID_TADS"

# Note to Agent: Stop here and supervise the process.
# Use `ps -p $PID_TADS` or check if the output files are fully generated.
