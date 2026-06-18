# Input QC And Background Gene Universe

## Required Checks

Check before enrichment:

- Input files exist and are non-empty.
- Gene ID column exists or the file is a one-column gene list.
- IDs are not all numeric unless numeric IDs are expected.
- Missing values are removed.
- Duplicated IDs are reported and collapsed.
- Foreground genes overlap the background.
- Background size is reasonable for the assay.
- Foreground genes map to the selected annotation database or `TERM2GENE`.
- Term sizes remain useful after background filtering.

## Background Rules

RNA-seq:

- Use all genes tested by DESeq2, edgeR, limma-voom, or another DE method after expression filtering.
- Do not use all annotated genes when many genes failed expression filtering.

Single-cell:

- Use genes detected in the Seurat/Scanpy object or genes tested by marker detection.
- For cluster markers, use the same testable universe for all clusters unless the marker method used cluster-specific filtering.

WGCNA:

- Use all genes entering WGCNA after filtering and variance selection.
- Do not use only module genes as background.

GWAS/candidate regions:

- Use all genes eligible under the same candidate-gene search rule.
- Example: if candidates are genes within 50 kb of significant SNPs, the background should be all genes that could have been selected by that 50 kb rule.

Pangenome and gene-family analysis:

- Use all gene families or all genes included in the comparative analysis.
- For expanded/contracted families, background is all tested families, not all genes in one genome.

Proteomics:

- Use all detected proteins or all proteins passing quantification filters.
- Do not use all predicted proteins unless the proteomics assay could detect them and they passed the same filtering universe.

## Thresholds

Default thresholds:

- FDR or adjusted p-value: `< 0.05`
- Minimum gene set size: `3` or `5`, depending on sample size.
- Maximum gene set size: `500`.
- Mapping rate warning: `40-70%`.
- Mapping rate blocked: `< 40%`.

## Up/Down Split

If direction is available:

- Create an upregulated gene list.
- Create a downregulated gene list.
- Optionally create an all-significant list.
- Report all runs, but interpret up and down separately first.

## Ranked Lists

For GSEA:

- Use a signed statistic such as Wald statistic, moderated t-statistic, signed `-log10(p)`, or log fold change when no better statistic exists.
- Include all testable genes, not only significant genes.
- Remove duplicated gene IDs deterministically, keeping the largest absolute statistic unless the user provides a different rule.

## Block Rather Than Guess

Block the run if no defensible background exists, the species is unknown, ID type cannot be inferred, or mapping rate is too low. A blocked report is preferable to a polished but invalid enrichment figure.
