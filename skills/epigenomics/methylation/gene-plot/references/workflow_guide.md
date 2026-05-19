# Methylation Profile Plot — Workflow Guide

Detailed code recipes for complex use cases, custom modifications, and integration
with other tools. Read this before any Tier 3 script modification.

## Table of Contents

1. [Data Preparation and Validation](#data-preparation-and-validation)
2. [Gene ID Handling](#gene-id-handling)
3. [Chromosome Name Mismatch](#chromosome-name-mismatch)
4. [Multi-Sample and Multi-Condition Designs](#multi-sample-and-multi-condition-designs)
5. [Custom Plot Layouts](#custom-plot-layouts)
6. [Color and Style Customization](#color-and-style-customization)
7. [Batch Processing](#batch-processing)
8. [Integrating with Other Tools](#integrating-with-other-tools)
9. [Troubleshooting Reference](#troubleshooting-reference)

---

## Data Preparation and Validation

### Checking bigWig integrity before plotting

```python
import pyBigWig

def validate_bw(path):
    try:
        bw = pyBigWig.open(path)
        chroms = bw.chroms()
        print(f"OK: {path}")
        print(f"  Chromosomes ({len(chroms)}): {list(chroms.keys())[:5]} ...")
        print(f"  Example stats: {bw.stats(list(chroms.keys())[0], 0, 10000)}")
        bw.close()
        return True
    except Exception as e:
        print(f"FAIL: {path} — {e}")
        return False

# Validate all samples
paths = ["/path/175l.CG.bw", "/path/175l.CHG.bw", "/path/175l.CHH.bw"]
all_ok = all(validate_bw(p) for p in paths)
```

### Checking TPM CSV format

```python
import pandas as pd

df = pd.read_csv("gene_tpm.csv")
print("Shape:", df.shape)
print("Columns:", df.columns.tolist())
print("First gene ID:", df.iloc[0, 0])
print("Missing values:", df.isnull().sum().sum())

# Check if gene IDs need cleaning (strip gene: prefix etc.)
sample_ids = df.iloc[:5, 0].tolist()
print("Sample gene IDs:", sample_ids)
```

### Validating GFF3 gene feature count

```python
import re

def count_gff_genes(gff_path):
    count = 0
    sample_ids = []
    with open(gff_path) as f:
        for line in f:
            if line.startswith("#"): continue
            parts = line.split("\t")
            if len(parts) < 9 or parts[2] != "gene": continue
            count += 1
            if len(sample_ids) < 5:
                m = re.search(r'ID=([^;]+)', parts[8])
                if m: sample_ids.append(m.group(1))
    print(f"Total gene features: {count}")
    print(f"Sample IDs (raw): {sample_ids}")

count_gff_genes("genome.gff3")
```

---

## Gene ID Handling

### Understanding the auto-cleaning logic

The script applies these transformations in order:

```python
import re

def clean_gene_id(x):
    x = str(x).strip()
    x = re.sub(r"^(gene:|transcript:|ID=|rna-)", "", x)  # strip prefixes
    x = re.sub(r";.*$", "", x)                            # strip trailing attrs
    x = re.sub(r"\.t\d+$", "", x)                         # strip isoform suffix
    return x

# Examples
clean_gene_id("gene:GT21.04g7G0018420")   # → GT21.04g7G0018420
clean_gene_id("GT21.04g7G0018420.t1")     # → GT21.04g7G0018420
clean_gene_id("ID=GT21.04g7G0018420;Name=someGene")  # → GT21.04g7G0018420
```

### Debugging mismatched gene IDs

```python
import pandas as pd, re

# Load GFF IDs
gff_ids = set()
with open("genome.gff3") as f:
    for line in f:
        if line.startswith("#"): continue
        parts = line.split("\t")
        if len(parts) < 9 or parts[2] != "gene": continue
        m = re.search(r'ID=([^;]+)', parts[8])
        if m:
            raw = m.group(1)
            cleaned = re.sub(r"^(gene:|transcript:|ID=|rna-)", "", raw)
            cleaned = re.sub(r";.*$", "", cleaned)
            cleaned = re.sub(r"\.t\d+$", "", cleaned)
            gff_ids.add(cleaned)

# Check your gene list against GFF IDs
my_genes = ["GT21.04g7G0018420", "SOMEOTHER_GENE"]
for g in my_genes:
    if g in gff_ids:
        print(f"✓ {g}")
    else:
        # Try to find similar
        close = [x for x in gff_ids if g[:10] in x][:3]
        print(f"✗ {g}  — similar: {close}")
```

### Custom ID cleaning for non-standard GFFs

If your GFF uses a different convention (e.g. `LOC_Os01g01010.1`), patch the
`clean_gene_id` function in your copied `plot_custom.py`:

```python
# AGENT-EDITABLE: gene ID cleaning — modify regex for your GFF format
def clean_gene_id(x):
    x = str(x).strip()
    x = re.sub(r"^(gene:|transcript:|ID=|rna-|LOC_)", "", x)
    x = re.sub(r";.*$", "", x)
    x = re.sub(r"\.\d+$", "", x)   # strip any trailing .1 .2 etc.
    return x
```

---

## Chromosome Name Mismatch

### Diagnosing the mismatch

```python
import pyBigWig

# Step 1: get bigWig chromosome names
bw = pyBigWig.open("merged.modcall.C.CG.bw")
bw_chroms = set(bw.chroms().keys())
bw.close()
print("BigWig chroms (first 10):", sorted(bw_chroms)[:10])

# Step 2: get GFF chromosome names
gff_chroms = set()
with open("genome.gff3") as f:
    for line in f:
        if not line.startswith("#"):
            gff_chroms.add(line.split("\t")[0])
print("GFF chroms (first 10):", sorted(gff_chroms)[:10])

# Step 3: find the mismatch pattern
common = bw_chroms & gff_chroms
print(f"Shared: {len(common)} | BW only: {len(bw_chroms-gff_chroms)} | GFF only: {len(gff_chroms-bw_chroms)}")
```

### Adding a chromosome name mapping

If GFF uses `Chr04g7` but bigWig uses `chr4g7`, add a mapping in your
copied `plot_custom.py` before calling `bw.values()`:

```python
# AGENT-EDITABLE: chromosome name mapping — add entries for your genome
CHROM_MAP = {
    "Chr04g7": "chr4g7",
    "Chr01g1": "chr1g1",
    # add more as needed
}

def resolve_chrom(chrom):
    return CHROM_MAP.get(chrom, chrom)

# Then replace all bw.values(chrom, ...) calls with:
# bw.values(resolve_chrom(chrom), ...)
```

---

## Multi-Sample and Multi-Condition Designs

### Grouping samples by condition for comparison plots

After running the standard script, compare conditions programmatically:

```python
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

summary = pd.read_csv("methylation_plot_summary.tsv", sep="\t")

# Define condition groups
groups = {
    "leaf_175": ["expr_175l"],
    "stem_175": ["expr_175s"],
    "leaf_18":  ["expr_18l"],
    "stem_18":  ["expr_18s"],
}

# Plot expression across conditions for each gene
fig, axes = plt.subplots(1, len(summary), figsize=(5 * len(summary), 4))
for ax, (_, row) in zip(axes, summary.iterrows()):
    vals = [row[cols[0]] for cols in groups.values()]
    ax.bar(list(groups.keys()), vals)
    ax.set_title(row["gene_id"])
    ax.set_ylabel("TPM")
    ax.tick_params(axis="x", rotation=30)

plt.tight_layout()
plt.savefig("condition_comparison.png", dpi=300)
```

### Adding a fifth sample to an existing run

Just append another `--sample` argument — no script changes needed:

```bash
python scripts/plot_methylation.py \
    [existing args] \
    --sample new_sample /path/CG.bw /path/CHG.bw /path/CHH.bw /path/comp.bed
```

### Plotting only specific contexts (e.g., CG only)

Copy the script and modify the `# AGENT-EDITABLE: subplot layout` section:

```python
# AGENT-EDITABLE: subplot layout — change to 2 rows if showing only CG+CHG
contexts = ["CG"]   # Remove CHG and CHH
fig = plt.figure(figsize=(6, 9))
gs = fig.add_gridspec(3, 1)
# Then update the subplot building accordingly
```

---

## Custom Plot Layouts

### Adding a fourth panel row (e.g., violin plots per region)

In your copied `plot_custom.py`, find the `# AGENT-EDITABLE: figure layout`
block and expand the gridspec:

```python
# AGENT-EDITABLE: figure layout — expand to 4 rows
fig = plt.figure(figsize=(13, 12))
gs = fig.add_gridspec(4, 3, height_ratios=[1.1, 0.9, 0.9, 0.9])

# Add new row axes
ax10, ax11, ax12 = [fig.add_subplot(gs[3, i]) for i in range(3)]

# Example: violin plot of body methylation per context
import seaborn as sns   # if missing, delegate: <install_tool env="bio_methylation_plot">seaborn</install_tool>
for ctx, ax in zip(contexts, [ax10, ax11, ax12]):
    body_vals = {s: region_means[ctx][s]["body"] for s in sample_keys}
    ax.violinplot(list(body_vals.values()), positions=range(len(sample_keys)))
    ax.set_xticks(range(len(sample_keys)))
    ax.set_xticklabels(sample_keys, rotation=30)
    ax.set_title(f"{ctx} body distribution")
```

### Saving individual panels as separate files

Add after the `fig.savefig()` call in `make_figure()`:

```python
# AGENT-EDITABLE: figure layout — export individual panels
for i, (ctx, ax) in enumerate(zip(contexts, axes_r0)):
    extent = ax.get_window_extent().transformed(fig.dpi_scale_trans.inverted())
    fig.savefig(os.path.join(outdir, f"{gene}.{ctx}_profile.png"),
                bbox_inches=extent.expanded(1.2, 1.4), dpi=dpi)
```

---

## Color and Style Customization

### Per-sample color palette

In your copied `plot_custom.py`, add a color dict before the plotting calls:

```python
# AGENT-EDITABLE: color per sample
SAMPLE_COLORS = {
    "175l": "#1f77b4",   # blue
    "175s": "#ff7f0e",   # orange
    "18l":  "#2ca02c",   # green
    "18s":  "#d62728",   # red
}

# Then in the profile loop, pass color=SAMPLE_COLORS.get(s, None)
for s in sample_keys:
    ax.plot(x, profiles[ctx][s], color=SAMPLE_COLORS.get(s),
            marker="o", lw=1, ms=3, label=s)
```

### Per-context color for regional mean bars

```python
# AGENT-EDITABLE: color per methylation context
CTX_COLORS = {
    "CG":  "#e74c3c",
    "CHG": "#3498db",
    "CHH": "#2ecc71",
}
```

### Using a white background for publication

Add before `fig.savefig()`:

```python
fig.patch.set_facecolor("white")
for ax in fig.get_axes():
    ax.set_facecolor("white")
```

---

## Batch Processing

### Process a large gene list in parallel

```python
from multiprocessing import Pool
import subprocess, os

def run_one_gene(gene):
    cmd = [
        "python", "scripts/plot_methylation.py",
        "--gff", "genome.gff3",
        "--tpm", "tpm.csv",
        "--outdir", "./out",
        "--genes", gene,
        "--sample", "175l", "CG.bw", "CHG.bw", "CHH.bw", "comp.bed",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[FAIL] {gene}: {result.stderr[-200:]}")
    return gene

with open("gene_list.txt") as f:
    genes = [line.strip() for line in f if line.strip()]

with Pool(processes=4) as pool:
    pool.map(run_one_gene, genes)
```

### Shell-based batching (no Python required)

```bash
# Split into batches of 20 genes
split -l 20 gene_list.txt batch_

for f in batch_*; do
    GENES=$(cat "$f" | tr '\n' ' ')
    python scripts/plot_methylation.py \
        --gff genome.gff3 --tpm tpm.csv \
        --outdir ./out \
        --genes $GENES \
        --sample 175l CG.bw CHG.bw CHH.bw comp.bed \
        --sample 18l  CG.bw CHG.bw CHH.bw comp.bed
    echo "Batch $f done"
done
```

---

## Integrating with Other Tools

### Feeding output into deepTools for comparison

```bash
# After generating bigWig profiles, compute matrix with deepTools
computeMatrix scale-regions \
    -S 175l.CG.bw 18l.CG.bw \
    -R gene_list.bed \
    --beforeRegionStartLength 2000 \
    --regionBodyLength 5000 \
    --afterRegionStartLength 2000 \
    -o matrix.gz

plotHeatmap -m matrix.gz -out heatmap.png
```

### Combining with DESeq2 results

```python
import pandas as pd

# Load methylation summary
meth = pd.read_csv("methylation_plot_summary.tsv", sep="\t")

# Load DESeq2 results
deseq = pd.read_csv("deseq2_results.csv", index_col=0)
deseq.index.name = "gene_id"
deseq = deseq.reset_index()

# Merge on gene_id
merged = meth.merge(deseq[["gene_id", "log2FoldChange", "padj"]], on="gene_id", how="left")

# Filter: significant DE genes with low body methylation
candidates = merged[
    (merged["padj"] < 0.05) &
    (merged["log2FoldChange"].abs() > 1)
]
print(f"Candidate genes: {len(candidates)}")
candidates.to_csv("candidates_meth_expr.tsv", sep="\t", index=False)
```

---

## Troubleshooting Reference

### Gene ID Handling

If cleaning still fails (gene listed as not found after running the script):

```python
# Manual debug: print all GFF IDs that partially match your gene
gene_to_find = "GT21.04g7G0018420"
with open("genome.gff3") as f:
    for line in f:
        if gene_to_find[:10] in line and "gene" in line:
            print(line.strip()[:120])
```

### Chromosome Name Mismatch

Full fix workflow for genomes where GFF uses `Chr04g7` but bigWig uses `chr04g7`:

```python
# Add this to your copied plot_custom.py near the top of main()
import pyBigWig

def build_chrom_map(bw_path, gff_path):
    """Auto-build a case-insensitive chrom name mapping."""
    bw = pyBigWig.open(bw_path)
    bw_chroms = {c.lower(): c for c in bw.chroms()}
    bw.close()
    gff_chroms = set()
    with open(gff_path) as f:
        for line in f:
            if not line.startswith("#"):
                gff_chroms.add(line.split("\t")[0])
    mapping = {}
    for gc in gff_chroms:
        if gc in bw_chroms.values():
            mapping[gc] = gc               # exact match
        elif gc.lower() in bw_chroms:
            mapping[gc] = bw_chroms[gc.lower()]   # case-insensitive match
    return mapping

CHROM_MAP = build_chrom_map("merged.CG.bw", "genome.gff3")
```

### Memory Error with Large Windows

For `--upstream 10000` across hundreds of genes, reduce memory footprint:

```python
# In plot_custom.py, process one gene at a time and close bigWigs between genes
for gene in args.genes:
    # Open bigWigs fresh for each gene
    opened = {}
    for s, info in samples.items():
        opened[s] = {ctx: pyBigWig.open(path) for ctx, path in info["bw"].items()}

    # ... process gene ...

    # Close immediately
    for s in opened:
        for ctx in opened[s]:
            opened[s][ctx].close()
```

### All Values NaN for Specific Genes

Possible causes and checks:

```python
# Check 1: Is the gene region within chromosome bounds?
bw = pyBigWig.open("CG.bw")
chrom_len = bw.chroms().get("Chr04g7", 0)
gene_end = 51939076
print(f"Chrom length: {chrom_len}, Gene end: {gene_end}, OK: {gene_end <= chrom_len}")

# Check 2: Is there any signal in the region at all?
vals = bw.stats("Chr04g7", 51936518, 51941076, type="mean", nBins=10)
print("Signal bins:", vals)

bw.close()
```
