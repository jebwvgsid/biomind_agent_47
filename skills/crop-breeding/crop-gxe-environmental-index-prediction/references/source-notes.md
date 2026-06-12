# CERIS-JGRA Source Notes

Verified on 2026-05-25.

## Paper

- Li, X., Guo, T., Wang, J. et al. "An integrated framework reinstating the environmental dimension for GWAS and genomic selection in crops." Molecular Plant 14(6), 874-887 (2021). DOI: https://doi.org/10.1016/j.molp.2021.03.010
- The paper uses field-observed traits, environmental profiles, and genome-wide SNPs in maize, wheat, and oat.
- Core idea: identify an environmental index as a combination of environmental parameter and growth window, regress trait values on that index, and use reaction-norm intercept and slope for GWAS/genomic selection.
- The framework is intended for complex trait dissection, phenotypic plasticity, GxE, and prediction of germplasm performance in new environments.

## Code

- Upstream repository: https://github.com/jmyu/CERIS_JGRA
- Repository contents include crop folders `0Maize`, `Wheat`, `Oat`, main `CERIS-JGRA.r`, `Sub_functions.r`, a vignette PDF, README, and GPL-3.0 license.
- Repository language is R.

## BioMind Implementation Notes

- This skill should keep environment-index search transparent: environmental variable, window, transformation, association strength, and biological interpretation must be reported.
- For genomic prediction, validate with environment-held-out or year-held-out folds whenever the objective is new-environment prediction.
- Exact package reproduction should use the upstream R scripts and vignette; record the commit and any changed paths.
