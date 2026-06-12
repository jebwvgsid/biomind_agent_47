# GP-WAITER Source Notes

Verified on 2026-05-25.

## Paper

- Li, J., Yu, L., Li, M. et al. "Leveraging weighted embedding and Transformer architecture to improve phenotype prediction of complex traits for crops." Nature Communications 17, 4427 (2026). DOI: https://doi.org/10.1038/s41467-026-71035-5
- Core method: GWAS p-values are transformed to SNP weights, genotype and weight tensors are combined with a Hadamard product, CNN layers extract local SNP patterns, and Transformer encoder blocks capture long-range dependencies.
- Reported evaluation uses Pearson correlation, MSE, RMSE, and MAE across soybean, maize, rice, and wheat datasets, with comparisons to rrBLUP, SVR, XGBoost, LightGBM, CNN, DNNGP, and Cropformer.
- Interpretation uses attention weights and SHAP-style analysis for SNP prioritization.

## Code

- Upstream repository: https://github.com/snowo-w/GP-WAITER
- The repository describes GP-WAITER as a CNN + Transformer model with GWAS-derived SNP weights and includes `train-test.py`, `requirements.txt`, a `model/GP_WAITER.so` compiled component, example inputs, example scripts, and expected output.
- Reported environment: Python 3.9, CUDA-compatible PyTorch, NumPy, pandas, scikit-learn, TensorBoard, and GPU execution; the paper reports PyTorch 1.13.0 for training and the GitHub repository supplies its own `requirements.txt`.
- README defaults include 200 epochs, batch size 32, Adam optimizer, learning rate 0.001, 3 encoder stacks, and multiple attention heads.
- Genotype preprocessing notes include PLINK 1.9 QC, Beagle 5.4 imputation, MAF < 0.01 filtering, missingness > 20% filtering, biallelic requirement, and LD pruning for soybean1861.
- SNP weights are calculated from GWAS p-values; the README mentions EMMAX for weight extraction.

## BioMind Implementation Notes

- This skill is a workflow wrapper and planning guide, not a vendored copy of upstream GP-WAITER.
- For rigorous evaluation, compute GWAS weights inside the training folds to avoid leakage.
- For exact reproduction, use the upstream GitHub release and archived version cited by the paper; record commit/release, environment, and all edited file paths.
