# DNNGP Multiomics Install And Data SOP

Use this file when the user asks for installation, model download, data download, or exact DNNGP reproduction. Upstream DNNGP is a separate TensorFlow project; do not present BioMind-local scripts as formal DNNGP execution.

## Execution Boundary

Do not use BioMind-local scripts for formal DNNGP analysis.
For biological conclusions, run the upstream TensorFlow DNNGP implementation or
label any adaptation explicitly as a non-reproduction.

## Upstream Exact Reproduction

Primary source:

- Paper: Wang et al., Molecular Plant 16(1), 279-293 (2023), DOI https://doi.org/10.1016/j.molp.2022.11.004
- Repository: https://github.com/AIBreeding/DNNGP

The upstream repository documents Python 3.9 and TensorFlow 2.6.0. Prefer Linux or WSL for reproducibility. TensorFlow 2.6 is old; GPU setup is sensitive to CUDA/cuDNN and platform ABI.

```bash
git clone https://github.com/AIBreeding/DNNGP.git external/DNNGP
cd external/DNNGP
mamba create -n dnngp_tf26 -c conda-forge python=3.9 pip -y
conda activate dnngp_tf26
pip install "tensorflow==2.6.0" numpy pandas scikit-learn scipy matplotlib openpyxl
python -c "import tensorflow as tf; print(tf.__version__); print(tf.config.list_physical_devices())"
```

Then read the OS-specific upstream manual and start scripts in the cloned repository before running. Record:

- commit hash: `git rev-parse HEAD`
- Python/TensorFlow versions
- edited input/output paths
- selected trait, omics block, fold design, batch size, dropout, patience, and tuning range

## Model/Data Download

- No general pretrained DNNGP model is expected. Train from the user's SNP, expression, metabolite, methylation, or other omics matrices and phenotype table.
- Upstream repository includes example input/output structure under `Input_files` and `Output_files`; use these as format references, not universal crop models.
- For user's data, require `sample_id` alignment across every matrix before normalization.
- For multiple traits, train trait-specific models unless a multi-task design is explicitly requested and validated.
