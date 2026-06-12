# Crop Smart Breeding Strategy Source Notes

Verified on 2026-05-25.

## Review and Perspective Sources

- Xu, Y. et al. "Smart breeding driven by big data, artificial intelligence, and integrated genomic-enviromic prediction." Molecular Plant (2022). DOI: https://doi.org/10.1016/j.molp.2022.09.001
  - Frames smart breeding as integrated genomic-enviromic prediction using genomics, phenomics, enviromics, multi-omics, big data, and AI.
- Yan, J. and Wang, X. "Machine learning bridges omics sciences and plant breeding." Trends in Plant Science 28(2), 199-210 (2023). DOI: https://doi.org/10.1016/j.tplants.2022.08.018
  - Frames ML as a bridge from biological knowledge and omics features to breeding decisions.
- Crossa, J. et al. "Expanding genomic prediction in plant breeding: harnessing big data, machine learning, and advanced software." Trends in Plant Science 30(7), 756-774 (2025). DOI: https://doi.org/10.1016/j.tplants.2024.12.009
  - Reviews genomic prediction expansion through big data, ML, phenomics, genomics, and software.

## BioMind Implementation Notes

- This strategy skill is a router, not an execution engine.
- Select downstream skills from the actual available data and desired validation target.
- Treat model choice as conditional on leakage-safe validation design.
