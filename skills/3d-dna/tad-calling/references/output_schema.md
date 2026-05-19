# TAD Output Schema

Use this reference before changing output columns or connecting the TAD outputs to downstream annotation.

## Native HiCExplorer Outputs

`{sample}_boundaries.bed`:

| Column | Meaning |
|---|---|
| 1 `chr` | Chromosome/contig name |
| 2 `start` | Boundary interval start |
| 3 `end` | Boundary interval end |
| 4 `ID` | Boundary ID |
| 5 `separation_score` | Boundary separation score |

`{sample}_domains.bed` commonly starts with:

| Column | Meaning |
|---|---|
| 1 `chr` | Chromosome/contig name |
| 2 `start` | TAD start |
| 3 `end` | TAD end |
| 4 `ID` | Domain ID |
| 5 `left_separation_score` | Separation score at left boundary |

`{sample}_score.bedgraph`:

| Column | Meaning |
|---|---|
| 1 `chr` | Chromosome/contig name |
| 2 `start` | Score interval start |
| 3 `end` | Score interval end |
| 4 `separation_score` | HiCExplorer TAD separation score |

## Combined Tables

`tad_domains_long.tsv`:

| Column | Meaning |
|---|---|
| `sample` | User-provided sample label |
| `resolution_bp` | Confirmed resolution |
| `chr` | Chromosome/contig |
| `start` | TAD start |
| `end` | TAD end |
| `id` | HiCExplorer domain ID |
| `length` | `end - start` |
| `source_file` | Parsed domains BED path |

`tad_boundaries_long.tsv`:

| Column | Meaning |
|---|---|
| `sample` | User-provided sample label |
| `resolution_bp` | Confirmed resolution |
| `chr` | Chromosome/contig |
| `start` | Boundary start |
| `end` | Boundary end |
| `id` | HiCExplorer boundary ID |
| `separation_score` | Boundary score if available |
| `source_file` | Parsed boundaries BED path |

`tad_domain_stats.tsv`:

| Column | Meaning |
|---|---|
| `sample` | User-provided sample label |
| `resolution_bp` | Confirmed resolution |
| `count` | Number of parsed domains |
| `max_length` | Maximum TAD length |
| `min_length` | Minimum TAD length |
| `mean_length` | Mean TAD length |
| `median_length` | Median TAD length |
| `total_length` | Sum of TAD lengths |
| `n_boundaries` | Number of parsed boundaries |

`tad_run_summary.tsv`:

| Column | Meaning |
|---|---|
| `sample` | User-provided sample label |
| `resolution_bp` | Confirmed resolution |
| `matrix` | Effective matrix URI sent to hicFindTADs |
| `out_prefix` | Native output prefix |
| `balanced` | `yes`, `no`, `skipped`, or blank |
| `status` | `success`, `failed`, or `dry_run` |
| `return_code` | hicFindTADs return code |
| `message` | Error/status message |
