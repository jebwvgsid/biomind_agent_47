# Cropformer Install And Data SOP

Use this file when the user asks for Cropformer installation, data preparation, or model download. Upstream Cropformer is PyTorch code; do not present BioMind-local scripts as formal Cropformer execution.

## Execution Boundary

Do not use BioMind-local scripts for formal Cropformer analysis.
For biological conclusions, run the upstream Cropformer implementation or label
any adaptation explicitly as a non-reproduction.

## Upstream Exact Reproduction

Primary source:

- Paper: Wang et al., Plant Communications 6(3), 101223 (2025), DOI https://doi.org/10.1016/j.xplc.2024.101223
- Repository: https://github.com/jiekesen/Cropformer

Use the upstream `cropformer.yml` environment if present:

```bash
git clone https://github.com/jiekesen/Cropformer.git external/Cropformer
cd external/Cropformer
mamba env create -f cropformer.yml
conda activate cropformer
python -c "import torch; print(torch.__version__); print(torch.cuda.is_available())"
```

Expected upstream input files are CSV genotype/phenotype splits such as `X_train.csv`, `y_train.csv`, `X_test.csv`, and `y_test.csv`. The README notes PED-to-matrix preprocessing and scripts such as `ped_to_matrix.py`, `model.py`, and `model_prediction.py`.

Before running, inspect and edit hard-coded paths in upstream scripts:

```bash
python ped_to_matrix.py
python model.py
python model_prediction.py
```

Record commit, conda environment export, marker filtering rules, train/test split, and any edited file paths.

## Model/Data Download

- No general pretrained Cropformer model is expected. Train the model from crop-specific genotype and phenotype data.
- Use upstream `data` only as a format reference if available.
- Preserve marker order and coordinates if attention weights, SHAP, SNP-to-gene mapping, or haplotype follow-up will be reported.
- If phenotype-informed marker selection is used, perform it inside each training fold.
