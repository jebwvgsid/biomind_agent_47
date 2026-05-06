# Methylation Profile Plot — API Reference

Low-level function signatures, parameter types, and edge cases.
Read this when modifying data-fetching or parsing logic in the script.

## Table of Contents

1. [pyBigWig API](#pybigwig-api)
2. [Script Internal Functions](#script-internal-functions)
3. [argparse Parameters](#argparse-parameters)
4. [Output Format Specifications](#output-format-specifications)

---

## pyBigWig API

### Opening and closing files

```python
import pyBigWig

bw = pyBigWig.open(path: str) -> pyBigWig.pyBigWig
bw.close()
```

Always close bigWig handles. Use try/finally or context managers in custom code:

```python
bw = pyBigWig.open("CG.bw")
try:
    # ... use bw ...
finally:
    bw.close()
```

### bw.chroms()

```python
bw.chroms() -> dict[str, int]
# Returns: {chromosome_name: chromosome_length}
# Example: {"Chr04g7": 52000000, "Chr01g1": 45000000, ...}
```

Always call this to verify chromosome names before fetching values.

### bw.values()

```python
bw.values(
    chrom: str,
    start: int,        # 0-based, inclusive
    end: int,          # 0-based, exclusive
    numpy: bool = False
) -> list[float | None] | np.ndarray
```

- Returns one value per base pair in `[start, end)`.
- Positions with no data return `None` (list mode) or `nan` (numpy mode).
- `start` must be `≥ 0` and `< chrom_length`.
- `end` must be `> start` and `≤ chrom_length`.
- **Always use `numpy=True`** for performance — 10–100× faster than list mode.

**Edge case handling in the script:**

```python
def fetch_mean(bw, chrom, start, end):
    start = max(0, int(start))           # clamp to 0
    end   = max(int(start) + 1, int(end))  # ensure end > start
    vals  = np.array(bw.values(chrom, start, end, numpy=True))
    vals  = vals[np.isfinite(vals)]      # drop NaN
    return float(np.nanmean(vals)) if vals.size else np.nan
```

### bw.stats()

```python
bw.stats(
    chrom: str,
    start: int = None,
    end: int = None,
    type: str = "mean",   # "mean" | "min" | "max" | "coverage" | "std"
    nBins: int = 1,
    exact: bool = False
) -> list[float | None]
```

Faster than `values()` for region statistics. Use for validation and diagnostics,
not for the main profile computation (which needs per-bin control).

```python
# Quick sanity check: is there any signal in a gene region?
stats = bw.stats("Chr04g7", 51936518, 51941076, type="mean", nBins=10)
print(stats)   # [0.02, None, 0.85, ...]  — None means no data in that bin
```

---

## Script Internal Functions

### clean_gene_id(x) → str

Strips standard GFF3 ID prefixes and isoform suffixes.

| Input | Output |
|-------|--------|
| `"gene:GT21.04g7G0018420"` | `"GT21.04g7G0018420"` |
| `"ID=GT21.04g7G0018420;Name=foo"` | `"GT21.04g7G0018420"` |
| `"GT21.04g7G0018420.t1"` | `"GT21.04g7G0018420"` |
| `"rna-XM_001234"` | `"XM_001234"` |

Modify the regex list if your GFF uses a different convention.

### parse_gff_gene(gff_file) → pd.DataFrame

Columns: `gene_id` (cleaned), `chr`, `start` (1-based), `end` (1-based), `strand`.

Note: `start` is stored as 1-based (GFF convention). The script converts to
0-based when computing regions:

```python
gstart = int(row["start"]) - 1   # convert to 0-based
gend   = int(row["end"])          # end is already exclusive in 0-based
```

### load_tpm(path, sample_keys) → pd.DataFrame

- Reads CSV, uses first column as gene ID.
- Matches columns by substring (case-insensitive).
- Groups by `gene_id` and sums duplicates (handles isoform-level input).
- Coerces non-numeric to 0.

**Column matching logic:**

```python
for key in sample_keys:
    for col in df.columns:
        if key.lower() in col.lower():
            matched[key] = col
            break   # first match wins
```

### fetch_binned(bw, chrom, start, end, nbins) → np.ndarray

Returns array of length `nbins`. Each element is the mean methylation in that bin.
Bins with no data → `np.nan`.

**Bin edge computation:**

```python
edges = np.linspace(start, end, nbins + 1, dtype=int)
# edges[i] to edges[i+1] defines bin i
# np.linspace guarantees edges[-1] == end exactly
```

### get_regions(row, upstream, downstream) → tuple[tuple, tuple, tuple]

Returns `(up, body, down)` each as `(start, end)` 0-based half-open intervals.

| Strand | upstream | body | downstream |
|--------|----------|------|------------|
| `+` | `[TSS-upstream, TSS)` | `[TSS, TES)` | `[TES, TES+downstream)` |
| `-` | `[TES, TES+upstream)` | `[TSS, TES)` | `[TSS-downstream, TSS)` |

Where TSS = `row["start"] - 1`, TES = `row["end"]` (0-based).

### get_compartment(bed_path, chrom, start, end) → str

Reads a BED4 file (chrom, start, end, label). Returns comma-joined unique labels
that overlap `[start, end)`. Returns `"NA"` if file missing or no overlap.

**BED format expected:**

```
Chr04g7    0      100000    B.2.1
Chr04g7    100000 200000    A.1.1
```

---

## argparse Parameters

### Required parameters

| Flag | Type | Description |
|------|------|-------------|
| `--gff` | str | Path to GFF3 annotation file |
| `--tpm` | str | Path to TPM CSV (first column = gene ID) |
| `--outdir` | str | Output directory (created if absent) |
| `--genes` | list[str] | One or more gene IDs |
| `--sample` | 5-tuple, repeatable | `LABEL CG.bw CHG.bw CHH.bw COMP.bed` |

### Optional parameters with defaults

| Flag | Default | Description |
|------|---------|-------------|
| `--upstream` | `2000` | bp upstream of TSS (or TES for − strand) |
| `--downstream` | `2000` | bp downstream of TES (or TSS for − strand) |
| `--nbins-up` | `20` | bins in upstream region |
| `--nbins-body` | `40` | bins in gene body |
| `--nbins-down` | `20` | bins in downstream region |
| `--dpi` | `300` | output PNG resolution |

### --sample format

```bash
# One --sample flag per sample, always 5 values in this order:
--sample LABEL CG_BW CHG_BW CHH_BW COMP_BED

# Example:
--sample 175l /data/175l.CG.bw /data/175l.CHG.bw /data/175l.CHH.bw /data/175l.comp.bed
```

Sample order in plots follows the order `--sample` flags appear on the command line.

---

## Output Format Specifications

### PNG panel layout

```
fig size: (13, 9) inches at default 300 dpi → 3900 × 2700 px
gridspec: 3 rows × 3 cols, height_ratios=[1.1, 0.9, 0.9]

[0,0] CG profile      [0,1] CHG profile     [0,2] CHH profile + legend
[1,0] CG bar          [1,1] CHG bar         [1,2] Expression bar
[2,0] CHH bar         [2,1] Compartment      [2,2] Gene metadata text
```

Profile x-axis positions:
- `0` to `NBINS_UP - 1` → upstream
- `NBINS_UP` to `NBINS_UP + NBINS_BODY - 1` → gene body
- `NBINS_UP + NBINS_BODY` to total − 1 → downstream

Dashed vertical lines at `NBINS_UP - 0.5` and `NBINS_UP + NBINS_BODY - 0.5`.

### TSV summary columns

| Column | Type | Description |
|--------|------|-------------|
| `gene_id` | str | Cleaned gene ID |
| `chr` | str | Chromosome name (from GFF) |
| `start0` | int | 0-based start position |
| `end` | int | End position |
| `strand` | str | `+` or `-` |
| `expr_{sample}` | float | TPM for each sample (NaN if not found) |
| `compartment_{sample}` | str | Compartment label(s), comma-joined, or `"NA"` |

Column order: fixed columns first, then `expr_*` in sample order, then `compartment_*` in sample order.
