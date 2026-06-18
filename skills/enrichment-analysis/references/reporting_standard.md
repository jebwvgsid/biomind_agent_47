# Reporting Standard

The enrichment report must answer:

1. What gene set or ranked list was analyzed?
2. What species, taxonomic group, genome version, and gene ID type were used?
3. What database or `TERM2GENE` was selected, and why?
4. What background universe was used?
5. What statistical method was used?
6. How many foreground and background genes were mapped?
7. What terms were significant after FDR correction?
8. What biological themes are supported by the data?
9. What limitations affect interpretation?

## Minimum Metadata

Include:

- Species and taxonomic group.
- Gene ID type.
- Genome/database version.
- Foreground gene count.
- Background gene count.
- Mapped gene count and mapping rate.
- Database used.
- Method: ORA, GSEA, fgsea, compareCluster-style per-group ORA, or custom method.
- Statistical test.
- Multiple-testing correction.
- Significance threshold.
- Number of significant terms.
- Exact commands and software versions.

## Interpretation Rules

- Do not over-interpret broad parent GO terms.
- Do not claim causality from enrichment alone.
- Do not interpret terms with raw p-values when FDR is non-significant.
- Do not hide low mapping rates, orthology-based inference, or missing background limitations.
- Keep directionality explicit: upregulated and downregulated gene lists can support different biology.

Prefer concise interpretations:

- "Upregulated genes are enriched in cell wall organization and lignin biosynthesis."
- "Downregulated genes are enriched in photosynthesis-related pathways."
- "This result is orthology-based and should be interpreted as functional inference, not direct curated annotation."

## Reproducibility

Always include:

- Input file paths.
- Output directory.
- Database and annotation versions.
- Command lines.
- Software versions.
- Generated output files.
