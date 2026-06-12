# Cropformer Source Notes

Verified on 2026-05-25.

## Paper

- Wang, H. et al. "Cropformer: An interpretable deep learning framework for crop genomic prediction." Plant Communications 6(3), 101223 (2025). DOI: https://doi.org/10.1016/j.xplc.2024.101223
- The model combines CNN with multi-head self-attention for crop phenotype prediction.
- The paper reports evaluations across more than 20 traits in maize, rice, wheat, foxtail millet, and tomato.
- The paper emphasizes attention weights, SHAP, and haplotype analysis for SNP/gene interpretation.
- The method uses genotype encodings and model interpretability for downstream SNP mining.

## Code

- Upstream repository: https://github.com/jiekesen/Cropformer
- Repository includes `cropformer.yml`, `model.py`, `model_prediction.py`, `ped_to_matrix.py`, `class`, and `data`.
- README describes a conda environment, PyTorch-compatible hardware, PED-to-matrix preprocessing, model training from `X_train.csv`, `y_train.csv`, `X_test.csv`, and `y_test.csv`, and prediction via `model_prediction.py`.
- README states Python 3.10/3.11 and optional hardware acceleration.

## BioMind Implementation Notes

- This skill is for the unweighted CNN-attention route. Use GP-WAITER when GWAS-derived weights are available and wanted.
- For exact Cropformer reproduction, use upstream scripts and edit file paths as documented in its README.
- Keep marker selection leakage-free when using MIC or any phenotype-informed filtering.
