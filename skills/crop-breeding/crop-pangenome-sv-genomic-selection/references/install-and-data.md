# Pangenome SV Genomic Selection Install And Data SOP

Use this file when the user asks for pangenome/SV genomic selection installation, GrapePan reproduction, variant data download, or model download. Formal analysis uses GrapePan or a species-specific pangenome/SV workflow plus a documented genomic prediction or heritability model.

This skill needs user SV/PAV data or downloaded/paper-specific pangenome resources with recorded source, subset, and checksum information.

## Upstream Exact Reproduction

Primary source:

- Paper: Liu et al., Nature Genetics 56, 2804-2814 (2024), DOI https://doi.org/10.1038/s41588-024-01967-5
- Repository: https://github.com/zhouyflab/GrapePan

Clone upstream code and create a general variant/pangenome tools environment:

```bash
git clone https://github.com/zhouyflab/GrapePan.git external/GrapePan
cd external/GrapePan
mamba create -n grapepan_tools -c bioconda -c conda-forge python=3.11 pandas numpy scikit-learn bcftools vcftools htslib samtools bedtools plink plink2 tabix -y
find genome pangenome population -maxdepth 2 -type f -name "*.sh" -print
```

Run only the scripts matching the user's crop, reference, and data type. The GrapePan repository is grapevine-specific; do not imply the same references, filtering thresholds, or trait definitions transfer directly to other crops.

Record repository commit, reference/pangenome version, sample set, variant caller, SV caller, QC thresholds, trait definitions, and selection-index weights.

## Model/Data Download

- No pretrained model is expected. Train genomic prediction or selection-index models from the user's SNP, SV, PAV, and phenotype data.
- Required data: SNP/indel VCF or matrix, SV VCF or matrix, optional gene presence/absence matrix, phenotype table, and annotation GFF3.
- Public GrapePan data may be split across the paper's data availability links and repository instructions; download only the files needed for the requested reproduction and record URLs/checksums.
- For non-grape crops, require species-appropriate pangenome references, annotations, SV calls, and phenotype definitions.
