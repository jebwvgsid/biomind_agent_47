# Conserved Transcriptome ML Install And Data SOP

Use this file when the user asks for installing the conserved transcriptome ML workflow, downloading OSF/GEO data, or reproducing the paper workflow. Formal analysis uses XGBoost and GENIE3, or the upstream OSF/GEO paper workflow.

This skill needs user expression data or downloaded/paper-specific transcriptome data with recorded source, subset, and checksum information.

## Upstream Exact Reproduction

Primary source:

- Paper: Cheng et al., Nature Communications 12, 5627 (2021), DOI https://doi.org/10.1038/s41467-021-25893-w
- OSF project: https://doi.org/10.17605/OSF.IO/AVJPH
- GEO accession: GSE152249

Python/R environment for XGBoost and GENIE3-style reproduction:

```bash
mamba create -n conserved_tx_ml -c conda-forge -c bioconda python=3.11 r-base r-data.table r-tidyverse bioconductor-genie3 bioconductor-geoquery -y
conda run -n conserved_tx_ml python -m pip install xgboost pandas numpy scikit-learn scipy
conda run -n conserved_tx_ml Rscript -e "library(GENIE3); library(GEOquery); sessionInfo()"
```

If Bioconductor packages are unavailable through conda on the target platform:

```r
install.packages(c("BiocManager", "data.table", "tidyverse"), repos = "https://cloud.r-project.org")
BiocManager::install(c("GENIE3", "GEOquery"))
```

## Model/Data Download

No pretrained model is expected. The paper workflow trains XGBoost models from expression-derived features and phenotype values.

Download OSF project files with a browser or an OSF client:

```bash
python -m pip install osfclient
osf -p avjph clone external/osf_avjph
```

Download GEO metadata/expression using GEOquery:

```r
library(GEOquery)
gse <- getGEO("GSE152249", GSEMatrix = TRUE)
saveRDS(gse, file = "external/GSE152249_geoquery.rds")
```

For raw SRA data, use NCBI SRA Toolkit after mapping GEO samples to SRR accessions:

```bash
mamba create -n sra_tools -c bioconda -c conda-forge sra-tools -y
conda run -n sra_tools prefetch SRR_ACCESSION
conda run -n sra_tools fasterq-dump SRR_ACCESSION --split-files --outdir external/sra_fastq
```

Record OSF file versions, GEO/SRA accessions, expression normalization method, ortholog table source, and whether conserved genes are within-species or cross-species.
