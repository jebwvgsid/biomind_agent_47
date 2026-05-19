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

The wrapper checks balance by opening the effective cooler URI and requiring a finite `weight` column in `clr.bins()`.

Manual check:

```python
import cooler

clr = cooler.Cooler("sample.mcool::/resolutions/40000")
print(clr.bins().columns)
weights = clr.bins()["weight"][:]
print(weights.notna().sum(), len(weights))
```

If the `weight` column is missing, balance the cooler:

```bash
cooler balance sample.cool
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
