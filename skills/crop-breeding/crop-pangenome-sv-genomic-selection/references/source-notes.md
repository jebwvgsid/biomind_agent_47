# Pangenome SV Genomic Selection Source Notes

Verified on 2026-05-25.

## Paper

- Liu, Z., Wang, N., Su, Y. et al. "Grapevine pangenome facilitates trait genetics and genomic breeding." Nature Genetics 56, 2804-2814 (2024). DOI: https://doi.org/10.1038/s41588-024-01967-5
- The study builds Grapepan v1.0 from 18 new phased T2T assemblies plus 11 published assemblies.
- It constructs a variant map with 9,105,787 short variants and 236,449 SVs from 466 grapevine cultivars.
- It integrates SVs into GWAS for 29 agronomic traits, reports 148 QTLs, and reports improved heritability estimates when SVs are included.
- It performs genomic prediction under a polygenic model and evaluates multi-trait breeding value.

## Code

- Upstream repository: https://github.com/zhouyflab/GrapePan
- Repository folders include `genome`, `pangenome`, and `population`, with Shell scripts.
- The paper and repository are grapevine-specific; BioMind should not assume the same reference, variant filtering, or trait definitions for other crops.

## BioMind Implementation Notes

- Treat SNPs, indels, SVs, and PAV features as separate evidence blocks until prediction comparison.
- For non-grape crops, require the user to provide species-appropriate pangenome references, annotation, and variant calls.
- For multi-trait selection, make trait weights and selection direction explicit.
