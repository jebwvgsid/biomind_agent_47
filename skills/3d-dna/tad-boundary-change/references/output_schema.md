# TAD Boundary Change Output Schema

Use this reference before modifying output columns or joining boundary-change results to annotations.

## Matching Semantics

The script compares boundaries per chromosome. A boundary is represented by one position:

- `midpoint`: `(start + end) // 2`, the default
- `start`: BED start
- `end`: BED end

For each comparison, candidate boundary pairs within `boundary_tol_bp` are sorted by absolute distance and greedily assigned one-to-one. This prevents one boundary from being counted as shared with multiple boundaries in the other sample.

Classes:

- `shared_exact`: matched boundaries with `delta_bp == 0`
- `shifted_within_tol`: matched boundaries with `abs(delta_bp) <= boundary_tol_bp` and non-zero shift
- `sample_a_specific`: boundary in sample A with no match in sample B
- `sample_b_specific`: boundary in sample B with no match in sample A

When `--reference SAMPLE` is used, `sample_a_specific` can be interpreted as reference-specific/lost in the compared sample, and `sample_b_specific` as gained in the compared sample.

## `tad_boundary_change_summary.tsv`

| Column | Meaning |
|---|---|
| `comparison` | `{sample_a}_vs_{sample_b}` |
| `sample_a` | First sample in the comparison |
| `sample_b` | Second sample in the comparison |
| `resolution_bp` | Shared resolution for the comparison, or `mixed` when explicitly allowed |
| `boundary_tol_bp` | Tolerance used for boundary matching |
| `boundary_tol_bins` | Tolerance in bins when derived from resolution |
| `n_a_boundaries` | Unique boundary positions in sample A |
| `n_b_boundaries` | Unique boundary positions in sample B |
| `n_matched` | One-to-one matched boundary pairs |
| `n_shared_exact` | Matched pairs with zero shift |
| `n_shifted_within_tol` | Matched pairs with non-zero shift within tolerance |
| `n_a_specific` | Boundaries present only in sample A under the tolerance |
| `n_b_specific` | Boundaries present only in sample B under the tolerance |
| `fraction_a_matched` | `n_matched / n_a_boundaries` |
| `fraction_b_matched` | `n_matched / n_b_boundaries` |
| `mean_abs_shift_bp` | Mean absolute shift among matched pairs |
| `median_abs_shift_bp` | Median absolute shift among matched pairs |
| `max_abs_shift_bp` | Maximum absolute shift among matched pairs |

## `tad_boundary_matches.tsv`

One row per matched pair.

| Column | Meaning |
|---|---|
| `comparison` | Pair label |
| `sample_a`, `sample_b` | Compared samples |
| `chrom` | Chromosome/contig |
| `a_start`, `a_end`, `a_position`, `a_id`, `a_score` | Boundary fields from sample A |
| `b_start`, `b_end`, `b_position`, `b_id`, `b_score` | Boundary fields from sample B |
| `delta_bp` | `b_position - a_position` |
| `abs_delta_bp` | Absolute shift |
| `match_class` | `shared_exact` or `shifted_within_tol` |
| `boundary_tol_bp` | Tolerance used |
| `a_source_file`, `b_source_file` | Input files |

## `tad_boundary_changes_long.tsv`

One row per boundary per comparison, including matched and unmatched boundaries. Use this table for plotting, downstream annotation, and gain/loss summaries.

| Column | Meaning |
|---|---|
| `comparison` | Pair label |
| `sample` | Boundary sample |
| `compared_to` | Other sample in the pair |
| `role` | `sample_a` or `sample_b` |
| `change_class` | `shared_exact`, `shifted_within_tol`, `sample_a_specific`, or `sample_b_specific` |
| `chrom`, `start`, `end`, `position`, `id`, `separation_score` | Boundary fields |
| `matched_sample` | Other sample if matched |
| `matched_position` | Matched boundary position if matched |
| `delta_bp`, `abs_delta_bp` | Shift relative to sample A |
| `resolution_bp` | Boundary row resolution when known |
| `boundary_tol_bp` | Tolerance used |
| `source_file` | Input file |

## `tad_boundary_sample_stats.tsv`

| Column | Meaning |
|---|---|
| `sample` | Sample label |
| `resolution_bp` | Resolved sample resolution when known |
| `resolution_source` | Where resolution came from |
| `source_files` | Boundary source files |
| `n_boundaries_input` | Number of parsed rows before de-duplication |
| `n_boundaries_unique_positions` | Number of unique `(chrom, position)` records used for matching |
| `n_duplicate_positions_removed` | Input rows removed by exact `(chrom, position)` de-duplication |
