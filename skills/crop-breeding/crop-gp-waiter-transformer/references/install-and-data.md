# GP-WAITER Install And Data SOP

Use this file when the user asks for GP-WAITER installation, compiled model components, GWAS-weight generation, or model download. Upstream GP-WAITER is PyTorch plus a compiled component; do not present BioMind-local scripts as formal GP-WAITER execution.

## Execution Boundary

Do not use BioMind-local scripts for formal GP-WAITER analysis.
For biological conclusions, run the upstream GP-WAITER implementation or label
any adaptation explicitly as a non-reproduction.

## Upstream Exact Reproduction

Primary source:

- Paper: Li et al., Nature Communications 17, 4427 (2026), DOI https://doi.org/10.1038/s41467-026-71035-5
- Repository: https://github.com/snowo-w/GP-WAITER

The upstream repository reports Python 3.9, PyTorch training, `requirements.txt`, `train-test.py`, example data, and a compiled `model/GP_WAITER.so` component. Prefer Linux/WSL with CUDA-compatible hardware.

```bash
git clone https://github.com/snowo-w/GP-WAITER.git external/GP-WAITER
cd external/GP-WAITER
mamba create -n gp_waiter -c pytorch -c nvidia -c conda-forge python=3.9 pytorch==1.13.0 pytorch-cuda=11.7 pip -y
conda activate gp_waiter
pip install -r requirements.txt
test -f model/GP_WAITER.so
python -c "import torch; print(torch.__version__); print(torch.cuda.is_available())"
```

If `model/GP_WAITER.so` fails to import, treat it as an upstream binary compatibility issue. Do not reimplement the hidden compiled internals unless the user explicitly asks for source-level reproduction and accepts the risk.

Before evaluation, generate GWAS weights inside the training fold or label the result as non-independent:

```bash
# Example only; choose the user's GWAS tool and covariates.
plink2 --bfile train_fold --glm hide-covar --pheno phenotype.tsv --out train_fold_gwas
```

Then run the upstream `train-test.py` after editing paths according to the upstream README.

## Model/Data Download

- No general pretrained GP-WAITER model is expected. Train from the user's genotype, phenotype, and GWAS p-value inputs.
- The repository includes example data and expected output; use them only as upstream format references.
- Required inputs: genotype matrix, phenotype table, GWAS p-values aligned to marker IDs, and optional marker annotation.
- Preserve the exact GWAS weighting rule, p-value cap, and marker order in the reproducibility report.
