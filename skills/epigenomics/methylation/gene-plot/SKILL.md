---
name: 甲基化基因绘图
description: >
  Plot CG/CHG/CHH DNA methylation profiles (upstream → gene body → downstream)
  from bigWig files, combined with gene expression (TPM) and chromatin compartment
  labels. Generates per-gene 3×3 PNG panels and a TSV summary. Use when user asks
  to visualize methylation, compare epigenetic patterns across samples, or integrate
  methylation with expression data for specific genes.
version: "2.0"
tags: [methylation, bigwig, epigenomics, visualization, porec, RNA-seq]
legacy_ids: [甲基化基因绘图, methylation_profile_plot]
runtime:
  default_env: bio_methylation_plot
  env_runner: conda run -n bio_methylation_plot python
  python_packages: [pyBigWig, pandas, numpy, matplotlib]
  install_command: conda create -n bio_methylation_plot -c conda-forge -c bioconda python=3.10 pybigwig pandas numpy matplotlib -y
---

# Methylation Profile Plot

## Overview

This skill visualizes per-gene DNA methylation patterns by combining three data types:

- **Methylation bigWig files** — CG, CHG, CHH contexts from bisulfite or nanopore data
- **Gene expression** — TPM values from bulk RNA-seq
- **Chromatin compartments** — A/B compartment labels from Hi-C or Pore-C

Each output PNG is a 3×3 panel grid:

| Row | Col 1 | Col 2 | Col 3 |
|-----|-------|-------|-------|
| 1 | CG line profile | CHG line profile | CHH line profile |
| 2 | CG regional mean bar | CHG regional mean bar | Expression (TPM) bar |
| 3 | CHH regional mean bar | Compartment labels | Gene metadata |

---
## Software Environment & Dependencies

This skill requires a Python 3 environment with specific data science and bioinformatics libraries.

### Required Python Packages
- **Python** >= 3.7
- **pyBigWig**: For reading and extracting methylation values from `.bw` files.
- **pandas**: For parsing TPM CSV files and outputting the summary TSV.
- **numpy**: For calculating binned means and handling NaN values.
- **matplotlib**: For generating the final PNG panel plots.

### Installation

It is highly recommended to use `conda` for installation, as `pyBigWig` requires C-library dependencies (like `libcurl` and `zlib`) which can sometimes fail to compile via basic `pip`.

**Option 1: Conda (Recommended)**


## When to Use This Skill

Use this skill when:
- Visualizing methylation levels around specific genes of interest
- Comparing methylation patterns between multiple samples or conditions
- Integrating methylation data with gene expression to study epigenetic regulation
- User mentions "甲基化图", "methylation profile", "bigwig", "CG/CHG/CHH"
- Working with Pore-C / nanopore methylation calling output (`.bw` files)

---

## Quick Start

Standard case: user provides gene IDs and file paths.

```bash
python scripts/plot_methylation.py \
    --gff /path/to/genome.gff3 \
    --tpm /path/to/gene_tpm.csv \
    --outdir /path/to/output \
    --genes GT21.04g7G0018420 GT21.07g3G0008680 \
    --sample 175l /path/CG.bw /path/CHG.bw /path/CHH.bw /path/comp.bed \
    --sample 175s /path/CG.bw /path/CHG.bw /path/CHH.bw /path/comp.bed \
    --sample 18l  /path/CG.bw /path/CHG.bw /path/CHH.bw /path/comp.bed \
    --sample 18s  /path/CG.bw /path/CHG.bw /path/CHH.bw /path/comp.bed
```

---

## Agent Decision Protocol

Before writing any code, follow this decision tree:

### Mandatory preflight — verify the runtime first

Before running the plotting command, execute a small dependency check with the
same `python` executable that will run `plot_methylation.py`:

```bash
conda run -n bio_methylation_plot python - <<'PY'
import pyBigWig, pandas, numpy, matplotlib
print("methylation plotting Python dependencies OK")
PY
```

If this fails, do not run the plot command yet. Install or select a Python
environment that contains `pyBigWig`, `pandas`, `numpy`, and `matplotlib`.
For this skill, the default reproducible environment is `bio_methylation_plot`.
After the environment is ready, run the plotting script with:
`conda run -n bio_methylation_plot python`.

### Tier 1 — Call script directly
**Trigger**: Standard use case. User provides gene list, bigWig paths, GFF, TPM.

Run the script exactly as shown in Quick Start above.

### Tier 2 — Modify parameters only
**Trigger**: User wants different window size, resolution, or bin count.

| User says | Add to command |
|-----------|----------------|
| "更大的上下游窗口" | `--upstream 5000 --downstream 5000` |
| "更细的分箱" | `--nbins-up 40 --nbins-body 80 --nbins-down 40` |
| "高分辨率图片" | `--dpi 600` |

Do **NOT** touch the script for these changes.

### Tier 3 — Read and patch the script
**Trigger**: User needs logic changes that parameters cannot cover.

Trigger keywords: "加一个" / "新增面板" / "改成小提琴图" / "只显示" / "去掉某个panel" / "颜色改成"

Action:
```bash
# 1. Read the script first
cat scripts/plot_methylation.py

# 2. Copy to working directory — NEVER edit files inside the skill directory
cp scripts/plot_methylation.py ./plot_custom.py

# 3. Edit plot_custom.py, then run it
python plot_custom.py [same args as above]
```

Look for `# AGENT-EDITABLE` comments in the script — these mark the safe zones
to modify (figure layout, bar chart logic, expression panel, window parameters).

---

## Core Concepts

### How profiles are computed

The gene locus is divided into three regions:

```
|--- upstream ---|--- gene body ---|--- downstream ---|
   NBINS_UP bins    NBINS_BODY bins    NBINS_DOWN bins
```

For each bin, the mean methylation value is fetched from the bigWig file.
The profile is the concatenation of all bin values left to right,
regardless of strand (strand only affects which end is "upstream").

### Strand handling

For `+` strand genes:
- upstream = `[TSS - 2000, TSS]`
- body = `[TSS, TES]`
- downstream = `[TES, TES + 2000]`

For `-` strand genes the upstream/downstream positions are swapped around TES/TSS.

### Gene ID cleaning

The script auto-strips common GFF3 prefixes:
- `gene:`, `transcript:`, `ID=`, `rna-`
- Trailing `.t1`, `.t2` isoform suffixes

So `gene:GT21.04g7G0018420` and `GT21.04g7G0018420` are treated identically.

### TPM column matching

Sample labels are matched to TPM CSV columns by substring, case-insensitive.
Sample label `175l` will match column `175l.sorted.bam` or `sample_175L_TPM`.

If no match is found, the script exits with an error listing available columns.

---

## Common Patterns

### Minimal two-sample comparison

```bash
conda run -n bio_methylation_plot python scripts/plot_methylation.py \
    --gff genome.gff3 --tpm tpm.csv \
    --outdir ./out \
    --genes GENE_A GENE_B \
    --sample control CG_ctrl.bw CHG_ctrl.bw CHH_ctrl.bw comp_ctrl.bed \
    --sample treated CG_trt.bw  CHG_trt.bw  CHH_trt.bw  comp_trt.bed
```

### Single gene, wide window

```bash
python scripts/plot_methylation.py \
    --gff genome.gff3 --tpm tpm.csv \
    --outdir ./out \
    --genes GT21.04g7G0018420 \
    --upstream 5000 --downstream 5000 \
    --sample 175l CG.bw CHG.bw CHH.bw comp.bed
```

### No compartment BED available

If compartment BED files don't exist, pass a dummy path — the script will
show "NA" for compartment labels without crashing:

```bash
--sample 175l CG.bw CHG.bw CHH.bw /nonexistent/path.bed
```

### Batch: plot all genes in a list file

```bash
GENES=$(cat gene_list.txt | tr '\n' ' ')
python scripts/plot_methylation.py \
    --gff genome.gff3 --tpm tpm.csv \
    --outdir ./out \
    --genes $GENES \
    --sample 175l CG.bw CHG.bw CHH.bw comp.bed
```

---

## Using the Core API (Jupyter / Custom Scripts)

If users want to call the underlying functions directly rather than running
the CLI script, the key building blocks are:

```python
import pyBigWig
import numpy as np

# Open a bigWig
bw = pyBigWig.open("/path/to/CG.bw")

# Fetch mean methylation over a region
vals = np.array(bw.values("Chr04g7", 51938518, 51939076, numpy=True))
vals = vals[np.isfinite(vals)]
mean_meth = float(np.nanmean(vals))

# Fetch binned profile (20 bins)
edges = np.linspace(51936518, 51941076, 21, dtype=int)
profile = []
for i in range(20):
    v = np.array(bw.values("Chr04g7", edges[i], edges[i+1], numpy=True))
    v = v[np.isfinite(v)]
    profile.append(float(np.nanmean(v)) if v.size else np.nan)

bw.close()
```

**Check available chromosomes first** — chromosome names in bigWig and GFF must match:

```python
bw = pyBigWig.open("CG.bw")
print(list(bw.chroms().keys())[:10])
```

---

## Output Files

| File | Description |
|------|-------------|
| `{OUTDIR}/{gene}.CG_CHG_CHH.methylation_expression.png` | Per-gene 3×3 panel (300 dpi) |
| `{OUTDIR}/methylation_plot_summary.tsv` | Summary table: expression + compartment per gene |

Summary TSV columns: `gene_id`, `chr`, `start0`, `end`, `strand`,
`expr_{sample}` × N samples, `compartment_{sample}` × N samples.

---

## Troubleshooting

### Gene not found in GFF

```
[WARN] GT21.04g7G0018420 not found in GFF — skipping
```

**Cause**: Gene ID format mismatch between your list and the GFF.

**Fix**: Check the actual IDs in the GFF:
```bash
grep "	gene	" genome.gff3 | head -5 | awk '{print $9}' | grep -o 'ID=[^;]*'
```

### Chromosome name mismatch (empty profiles / RuntimeError)

**Symptom**: All methylation values are NaN, or `RuntimeError: Invalid interval`.

**Cause**: BigWig uses `chr1` but GFF uses `Chr01` (or vice versa).

**Fix**:
```python
import pyBigWig
bw = pyBigWig.open("CG.bw")
print(list(bw.chroms().keys())[:5])
bw.close()
```
```bash
awk '$3=="gene"{print $1}' genome.gff3 | sort -u | head -5
```

### TPM column not detected

```
ValueError: No sample columns matched ['175l', '175s']
```

**Fix**: Print actual column names:
```python
import pandas as pd
print(pd.read_csv("gene_tpm.csv").columns.tolist())
```
Then adjust your `--sample` labels to match a substring of the column names.

### pyBigWig installation fails

```bash
conda install -c bioconda pyBigWig   # recommended
pip install pyBigWig                 # fallback (needs libBigWig headers)
```

### Memory error with many genes

Process genes in batches:
```bash
split -l 10 gene_list.txt batch_
for f in batch_*; do
    python scripts/plot_methylation.py --genes $(cat $f) [other args]
done
```

---

## Reference Documentation

For detailed parameter and function documentation beyond this guide:

- **API reference** (`references/api_reference.md`): pyBigWig API, fetch function
  signatures, parameter types. Read when modifying data-fetching logic.
- **Workflow guide** (`references/workflow_guide.md`): Extended examples for
  complex multi-condition designs, normalization options, custom color palettes.
  Read when handling non-standard experimental designs.
