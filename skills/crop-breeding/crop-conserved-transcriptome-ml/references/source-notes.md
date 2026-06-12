# Conserved Transcriptome ML Source Notes

Verified on 2026-05-25.

## Paper

- Cheng, C.-Y., Li, Y., Varala, K. et al. "Evolutionarily informed machine learning enhances the power of predictive gene-to-phenotype relationships." Nature Communications 12, 5627 (2021). DOI: https://doi.org/10.1038/s41467-021-25893-w
- The workflow uses conserved nitrogen-responsive DEGs in Arabidopsis and maize to reduce transcriptome feature dimensionality for NUE prediction.
- XGBoost regression is used for phenotype prediction, with internal cross-validation and leave-genotype-out testing.
- XGBoost feature importance is used to prioritize influential genes.
- GENIE3 is used to infer TF regulatory networks for candidate transcription factor prioritization.

## Code

- OSF project: https://doi.org/10.17605/OSF.IO/AVJPH
- Nature Communications code availability points to the Coruzzi lab Open Science Framework, not a GitHub repository.
- GEO data accession in the paper: GSE152249.

## BioMind Implementation Notes

- This skill should generalize the strategy: conserved response gene selection plus interpretable ML.
- For crops without a model species comparison, use within-species conserved response genes across genotypes or environments and label the analysis accordingly.
- Always compare against at least one non-conserved or baseline feature set when claiming that conservation improves prediction.
