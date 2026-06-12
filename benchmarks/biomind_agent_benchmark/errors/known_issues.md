# Known Benchmark And Test-Data Issues

## Environment Dependencies

- `generate_msprime_fixtures.py` requires `msprime`, `tskit`, and `numpy`.
- Crop prediction runners require `scikit-learn`, `numpy`, and `pandas`.
- Public demo validation requires `scipy`, `pyyaml`, and, for `.cool` files, `cooler`.

## Data Source Boundaries

- `msprime` generates genetic variation, haplotypes, and VCF-like data.
- BioMind derives phenotype values and GWAS-like p-values from the simulated genotype matrix. These are controlled truth labels, not real GWAS results from PLINK/GEMMA.
- The deprecated toy generators under `scripts/legacy/` are not active benchmark data sources.

## Path Changes

- Active generator: `scripts/generate/generate_msprime_fixtures.py`.
- Active public downloader: `scripts/download/download_public_demo_fixtures.py`.
- Active validators: `scripts/validate/`.
- Older direct paths under the benchmark root have been replaced by structured subdirectories.

