---
name: loop-classification
description: >
  Classify chromatin loop anchors by overlap with histone marks, regulatory
  annotations, or other BED tracks. Use after loop calling when the user asks
  for loop classification, enhancer-promoter loops, promoter-promoter loops,
  H3K27ac/H3K4me1/H3K4me3 loop annotation, or BED-based loop anchor categories.
  Do not use for detecting loops from contact matrices; use loop-calling first.
legacy_ids: [loops-classification, loop-anchor-annotation, enhancer-promoter-loops, Loop分类]
compatibility: Pure Python classification is built in; bedtools is optional for large BED intersections.
---

# Loop Classification By Anchor Marks

Use this skill to classify existing loop calls by annotating both anchors with histone modifications or other BED tracks.

The input loop table is expected to be a Mustache-like TSV with anchor columns:

```text
BIN1_CHR  BIN1_START  BIN1_END  BIN2_CHROMOSOME  BIN2_START  BIN2_END
```

The same workflow is useful for H3K27ac/H3K4me1/H3K4me3 histone marks, ATAC peaks, CTCF peaks, promoters, enhancers, or custom regulatory annotations.

## When To Use

- User asks for `loop分类`, loop anchor annotation, enhancer-promoter loops, promoter-promoter loops, or histone mark categories.
- Loop calls already exist from Mustache or an equivalent caller.
- Mark files are BED-like files with at least `chrom`, `start`, and `end`.

Do not use this for the original loop detection step. Use `skills/3d-dna/loop-calling` first.

## Quick Start

Repeated `--mark` arguments:

```bash
python skills/3d-dna/loop-classification/scripts/classify_loops_by_marks.py \
  --loops Loops/Mustache/10000/leaf.tsv \
  --mark H3K27ac LoopsClassification/H3K27ac.bed \
  --mark H3K4me1 LoopsClassification/H3K4me1.bed \
  --mark H3K4me3 LoopsClassification/H3K4me3.bed \
  --out-dir Loops/Classification/10000/leaf \
  --fraction-target mark \
  --overlap-fraction 0.5
```

Mark sheet:

```bash
python skills/3d-dna/loop-classification/scripts/classify_loops_by_marks.py \
  --loops Loops/Mustache/10000/leaf.tsv \
  --mark-sheet skills/3d-dna/loop-classification/examples/marks.tsv \
  --out-dir Loops/Classification/10000/leaf
```

## Mark Sheet Format

```tsv
mark	path
H3K27ac	LoopsClassification/H3K27ac.chrs1to5.bed
H3K4me1	LoopsClassification/H3K4me1.chrs1to5.bed
H3K4me3	LoopsClassification/H3K4me3.chrs1to5.bed
```

## Classification Logic

The script annotates `BIN1` and `BIN2` separately, then combines both anchor roles into a loop class.

Default role mapping:

| Anchor marks | Anchor role |
|---|---|
| Contains `H3K4me3` | `promoter` |
| Contains both `H3K27ac` and `H3K4me1` | `active_enhancer` |
| Contains `H3K27ac` or `H3K4me1` | `enhancer` |
| Contains other marks only | `marked` |
| No overlapping marks | `unmarked` |

Loop class examples:

- `promoter-promoter`
- `promoter-enhancer`
- `active_enhancer-promoter`
- `enhancer-enhancer`
- `marked-unmarked`
- `unmarked-unmarked`

For non-histone or custom annotations, use `bin1_marks`, `bin2_marks`, and `mark_pair` columns directly. The role mapping is a convenience layer, not the only result.

## Overlap Policy

The teaching example used:

```bash
bedtools intersect -a loops_bin1.bed -b makers.bed6 -F 0.5 -wa -wb
```

That means at least 50% of the BED mark interval must overlap the loop anchor. The script preserves this behavior by default:

```text
--fraction-target mark --overlap-fraction 0.5
```

Other useful policies:

| Policy | Meaning |
|---|---|
| `--fraction-target mark` | fraction is calculated on the mark interval, matching bedtools `-F` |
| `--fraction-target anchor` | fraction is calculated on the loop anchor, matching bedtools `-f` |
| `--fraction-target either` | either anchor or mark fraction can pass |
| `--fraction-target both` | both anchor and mark fractions must pass |

## Outputs

```text
Loops/Classification/10000/leaf/
├── loops_anchors.tsv
├── classification_summary.tsv
├── anchor_mark_overlaps.tsv
├── loops_bin1.bed
├── loops_bin2.bed
└── marks.bed6
```

Important columns in `loops_anchors.tsv`:

| Column | Meaning |
|---|---|
| `loop_id` | Stable loop ID generated from row order unless an ID column exists |
| `bin1_marks`, `bin2_marks` | Semicolon-separated mark types per anchor |
| `bin1_role`, `bin2_role` | Convenience role labels |
| `loop_class` | Combined role class |
| `mark_pair` | Combined raw mark sets for both anchors |
| `distance_bp` | `BIN2_START - BIN1_START` |

## Workflow

1. Confirm loop calls and mark BED files use the same genome assembly and chromosome naming convention.
2. Split loop anchors into `BIN1` and `BIN2` BED records.
3. Intersect each anchor set with the combined mark BED.
4. Aggregate mark types by `loop_id` and anchor side.
5. Assign anchor roles and loop classes.
6. Report class counts and note any genome-build or chromosome-prefix mismatch.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| All loops are `unmarked-unmarked` | Genome build or chromosome names differ | Check `chr1` vs `Chr01` vs `1`, and confirm mark BED assembly |
| Too many multi-mark anchors | Broad peaks or low overlap threshold | Increase `--overlap-fraction` or use `--fraction-target anchor` |
| Bedtools command fails | `bedtools` not installed or not on PATH | Use default Python engine or pass `--engine python` |
| Many-to-many warning in R join | Expected when one anchor overlaps multiple peaks | Aggregate marks by `loop_id` before classifying |

## Related Skills

- `skills/3d-dna/loop-calling` - call loops and differential loops with Mustache.
- `skills/open-chromatin/modkit-cpu` - generate open-chromatin-like BED inputs from Nanopore modBAM.
- `skills/epigenomics/methylation/gene-plot` - integrate genomic features with methylation or gene-centered plots.
