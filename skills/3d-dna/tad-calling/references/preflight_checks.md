# TAD Preflight Checks

Read this before running real data when resolution, matrix correction, or sample sheet structure is uncertain.

## Required Checks

1. Matrix file exists.
2. Resolution is explicit for `.mcool`.
3. All samples in one comparison use the same resolution.
4. Cooler has a usable balance weight column, usually `weight`.
5. `hicFindTADs` is on `PATH`.

Use the shared environment `hicexplore` for TAD calling, differential TAD, boundary statistics, and Hi-C heatmap-related skills. Verify it before any install attempt:

```bash
conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py --help
```

If the check passes, do not install anything. If the environment or a software dependency is missing, do not install directly from the execution step. Delegate to the installer for the same shared environment:

```text
<install_tool env="hicexplore">hicexplorer</install_tool>
<install_tool env="hicexplore">cooler</install_tool>
<install_tool env="hicexplore">matplotlib</install_tool>
```

Re-run the healthcheck after installer delegation.

## Check Available Resolutions

```bash
conda run -n hicexplore python -c "import cooler; print(cooler.fileops.list_coolers('sample.mcool'))"
```

Avoid bare `cooler ...` commands in managed runs unless `which cooler` confirms the command is not a stale `bin_shims/cooler`. Prefer `conda run -n hicexplore cooler ...` when a CLI call is needed.

Typical output:

```text
['/resolutions/10000', '/resolutions/40000', '/resolutions/100000']
```

Ask the user which one to use. Do not choose automatically.

## Check Balance

The wrapper checks balance by opening the effective cooler URI and requiring a finite `weight` column in `clr.bins()`. During normal TAD calling, the wrapper automatically runs `cooler balance` on the effective URI if the `weight` column is missing or has no finite values, then re-checks before calling `hicFindTADs`. During `--preflight-only`, the wrapper only reports balance status and does not modify the matrix.

Manual check:

```python
import cooler

clr = cooler.Cooler("sample.mcool::/resolutions/40000")
print(clr.bins().columns)
weights = clr.bins()["weight"][:]
print(weights.notna().sum(), len(weights))
```

Do not use `clr.bins()["weight"].dropna()` or `clr.bins()["weight"].notna()`
directly. `clr.bins()["weight"]` is a Cooler selector until it is sliced with
`[:]`, so pandas-like methods are only valid after `weights =
clr.bins()["weight"][:]`. Do not use `cooler attrs | grep weight` to check
balance weights because `weight` is a bins-table column, not a top-level cooler
attribute.

Compact shell check for multiple matrices:

```bash
conda run -n hicexplore python - <<'PY'
import cooler

paths = [
    "sample_a.mcool::/resolutions/40000",
    "sample_b.mcool::/resolutions/40000",
]
for path in paths:
    clr = cooler.Cooler(path)
    columns = list(clr.bins().columns)
    print(path, "columns=", columns)
    if "weight" not in columns:
        print(path, "weight=MISSING")
        continue
    weights = clr.bins()["weight"][:]
    print(path, "weight=", weights.notna().sum(), "/", len(weights))
PY
```

If the `weight` column is missing during normal TAD calling, use the wrapper's default auto-balance behavior:

```bash
conda run -n hicexplore python skills/3d-dna/tad-calling/scripts/call_tads.py --sample SAMPLE sample.mcool --resolution-bp 40000 --out-root TADs/HiCExplorer
```

For `.mcool`, balance must be applied to the relevant resolution or generated upstream during matrix creation.

## When To Skip Balance Check

Only use `--skip-balance-check` if the user confirms one of these:

- The matrix was corrected by a non-cooler workflow and HiCExplorer accepts it.
- The effective correction is stored under a different mechanism not visible as a `weight` bin column.
- The user only wants exploratory output and accepts the risk.

Record the decision in the run notes or final response.

## Sample Sheet Validation

Required columns:

- `sample`
- `matrix`

Optional column:

- `resolution_bp`

The script prints available columns if required fields are missing. Duplicate sample labels are rejected after sanitization because they would write to the same output prefix.
