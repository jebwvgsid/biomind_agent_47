# DNNGP Source Notes

Verified on 2026-05-25.

## Paper

- Wang, K., Abid, M.A., Rasheed, A., Crossa, J., Hearne, S., and Li, H. "DNNGP, a deep neural network-based method for genomic prediction using multi-omics data in plants." Molecular Plant 16(1), 279-293 (2023). DOI: https://doi.org/10.1016/j.molp.2022.11.004
- The article presents DNNGP for plant genomic prediction using diverse omics data.
- Compared models include GBLUP, LightGBM, SVR, DeepGS, and DLGWAS.
- Key neural-network components: multilayer DNN, batch normalization, ReLU, dropout, early stopping, and local hyperparameter tuning.
- The authors state that DNNGP can handle SNP and other omics features and is most useful as data size increases.

## Code

- Upstream repository: https://github.com/AIBreeding/DNNGP
- README states that DNNGP is a Python 3.9 / TensorFlow 2.6.0 project for genome-wide prediction from multi-omics data in plants and animals.
- Repository folders include `Input_files`, `Output_files`, `Scripts`, and `Tuning_hyperparameters`.
- The project provides Windows, macOS, and Linux manuals plus start scripts.
- The 2025-01-21 change log mentions `DNNGP_OPN.py` in `Tuning_hyperparameters` for batch tuning of multiple phenotypes.
- README tuning guidance emphasizes large batch sizes where hardware allows, early stopping, patience, dropout1/dropout2 searches, and local machine tuning.

## BioMind Implementation Notes

- This skill should be used as a comparative prediction workflow, not as a claim that BioMind has vendored DNNGP.
- Prefer baseline-first execution. DNNGP is an advanced model after leakage-safe splits, normalization, and baseline evaluation are stable.
- For exact reproduction, run upstream DNNGP in its own environment and record version, manual, input file format, and edited configuration paths.
