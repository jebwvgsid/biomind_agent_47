# BioMind Benchmark Data Sources And Simulators

This note separates public downloadable data from active professional simulation.
Active benchmark data should be either msprime-generated professional genetic
simulation or downloaded public/database subsets with source metadata.

## Recommended Policy

1. Use msprime professional simulation for crop/popgen smoke tests.
2. Keep public real datasets behind an explicit download step.
3. Cache public data under a non-git data directory and commit only manifests,
   checksums, and subsetting scripts.
4. Prefer small derived matrices/tables over raw FASTQ/BAM for agent evaluation.
5. Treat live network access as optional; benchmark scoring should be reproducible
   from cached fixtures.
6. Keep hand-written toy NumPy/Pandas fixtures out of the active benchmark path.
7. Data-backed tasks must include explicit fixture paths in the prompt and must
   score both skill selection and result interpretation.
8. Use `availability: planned` for server-side or later-copied fixtures that are
   not present in the current workspace; switch to `available` only when the
   files exist at the declared benchmark path.

## Public Data Candidates

| BioMind area | Public source | Why useful | Benchmark use |
| --- | --- | --- | --- |
| scRNA ingest/QC | 10x Genomics PBMC3k, used by Seurat tutorials | Small, standard 10x matrix; good for Seurat object construction, QC, clustering | Optional mini-real scRNA benchmark; can downsample cells/genes |
| scRNA contrast | GEO GSE96583 / Kang PBMC IFN-beta dataset | Real control/stimulated PBMC scRNA design | Manual/mini-real DE benchmark after strong subsetting |
| Hi-C/TAD/loops | Open2C cooltools example `HFF_MicroC` via `cooltools.download_data()` | Small Micro-C `.mcool` example with multiple resolutions | Optional TAD/loop preflight; still subset and cache |
| Public database routing | NCBI GEO/Entrez and Ensembl REST | Small metadata/FASTA responses, no large downloads required | Routing-only and retrieval tests |
| Crop genomic prediction | BGLR package wheat dataset; G2F public maize GxE data | BGLR is package-bundled; G2F is realistic but large | Use msprime for smoke; use BGLR/G2F only after subsetting and checksums |
| Crop GxE | CLIM4OMICS / G2F 2014-2021 public maize data | Multi-environment phenotypes plus weather/envirotype tables and genotype-like marker matrices | Active public subset for GxE reaction-norm benchmark after strong subsetting |
| Crop pangenome/SV | Maize pan-genome SV resources and pan-transcriptome expression data | Public SV VCFs and expression-derived gene presence/absence proxy features | Active public SV/ePAV subset; ePAV is a derived expression presence proxy, not direct assembly gene PAV |
| Crop metabolomics | Metabolomics Workbench ST001947 maize shoot metabolite profiling | Real Zea mays GC-TOFMS metabolite abundance table with sample metadata | Active standalone metabolite matrix subset; not guaranteed sample-aligned with CLIM4OMICS genotype/expression fixtures |
| WGS truth / annotation readiness | GIAB HG002 NIST v4.2.1 GRCh38 truth set | Professional benchmark truth VCF and high-confidence regions | Active readiness fixture; full WGS calling still requires reads, reference FASTA, and annotation/snpEff database |
| ONT methylation / modkit | ONT Methylation Benchmarking Datasets, E. coli 5mC modBAM plus bisulfite truth | Public modBAM/reference/truth data suitable for `modkit pileup` and MM/ML tag checks | Planned public fixture for 5mC methylation pileup, truth overlap, correlation, and RMSE result-choice scoring |
| Methylation tracks | ENCODE WGBS bigWig/bedMethyl data standards and portal files | Public methylation signal tracks exist | Prefer generated tiny bedGraph/bigWig for CI; public data optional |
| Long reads / assembly | Public long-read datasets exist, but often large | Useful for realistic assembly only after subsetting | Prefer PBSIM3/NanoSim synthetic reads for smoke |

## Simulator Candidates

| BioMind area | Simulator/tool | Output type | Recommended use |
| --- | --- | --- | --- |
| scRNA counts | Splatter | count matrices / SingleCellExperiment | Candidate only; not active until explicitly promoted |
| realistic sc/spatial omics | scDesign3 | synthetic single-cell/spatial omics learned from real data | Later version for harder scRNA benchmarks |
| bulk RNA-seq | Polyester | FASTQ reads with known differential expression | Use when testing upstream RNA-seq wrappers; otherwise generate count/TPM tables directly |
| crop breeding / genomic prediction | AlphaSimR | genotype, phenotype, breeding values | Candidate only; not active until explicitly promoted |
| population genetics / VCF | msprime | VCF/tree sequence/variant matrices | Active generator for professional simulated crop/popgen fixtures |
| forward population genetics | SLiM or simuPOP | richer evolutionary scenarios | Optional hard-mode popgen fixtures; not needed for first CI |
| short-read WGS/RNA-seq | ART or wgsim | FASTQ/SAM-like simulated reads | Use for alignment/calling smoke tests if raw reads are needed |
| long reads / assembly | PBSIM3, NanoSim | PacBio/ONT-like long reads | Generate tiny genome assembly fixtures |
| variant benchmark | VarSim or VarBen | reads/VCF with known variants | Optional for future variant-calling benchmark tasks |
| Hi-C read simulation | Sim3C / FreeHi-C-style tools | Hi-C read pairs/contact data | More complex than needed; first generate tiny `.cool/.mcool` matrices directly |

## Practical Fixture Plan

### Phase 1: active professional msprime fixtures

- Generate coalescent ancestry and mutation simulation with `msprime`.
- Export VCF, haplotypes, genotype dosage matrix, phenotype table, GWAS-like
  truth labels, marker annotation, and simulation truth metadata.
- Use `scripts/generate/generate_msprime_fixtures.py`.

### Phase 2: public-data downloader

- Download PBMC3k and create a fixed downsampled subset.
- Extract the public cooler example `.cool` matrix and keep only one chromosome;
  this gives a small runnable `.cool` fixture without requiring large Hi-C files.
- Download UCSC hg38 chrM as a tiny real genome FASTA fixture.
- Keep a generated `answer_key.yaml` beside each public subset with expected
  dimensions, checksums, and summary statistics.
- Download or cache cooltools `HFF_MicroC` and keep only a small chromosome window
  as a later hard-mode replacement when larger downloads are acceptable.
- Keep GSE96583/G2F as manual benchmark candidates unless a small stable subset
  is created and checksummed.

### Phase 3: runner integration

- Keep active manifests under `manifests/`.
- Use `scripts/run/run_msprime_crop_gp_smoke.py` for crop/popgen smoke tests.
- Keep deprecated toy generators under `scripts/legacy/` only for traceability.

### Phase 4: extended public fixtures

- CLIM4OMICS/G2F is used for a small maize GxE subset with
  `phenotype_multi_environment.tsv`, `genotype_matrix.tsv`,
  `environment_timeseries.tsv`, and `growth_stage_window.tsv`.
- Maize pan-genome SV data are subsetted to a small `sv.vcf` for pangenome/SV
  genomic selection checks.
- Maize pan-transcriptome FPKM data are converted into an expression
  presence/absence proxy matrix (`pangenome_gene_presence_absence.tsv`) with
  FPKM >= 1 encoded as present. This is a benchmark ePAV proxy and must not be
  described as direct assembly-derived gene PAV.
- Metabolomics Workbench ST001947 is converted into a standalone
  `metabolite_matrix.tsv`. It supplies real maize metabolite abundances, but
  sample IDs are not assumed to align with the CLIM4OMICS or pan-transcriptome
  genotype fixtures.
- GIAB HG002 v4.2.1 GRCh38 truth VCF and high-confidence BED are used for WGS
  benchmark-readiness checks. The current fixture does not include FASTQ reads,
  a reference FASTA, or GFF3/snpEff database files.
- ONT basemod E. coli 5mC is used as a planned `modkit pileup` benchmark once
  `Ecoli_WT_5kHz_sup_v5.2r1_5mC.bam`, `ecoli.fa`, bisulfite truth, and
  `answer_key.yaml` are copied into
  `fixtures_public/ont_methylation_modkit/`. It is a 5mC methylation benchmark,
  not a 6mA open-chromatin benchmark.

## Data-Backed Result-Choice Evaluation

BioMind v0.3 data-backed open tasks use the BioMed-AQA prompt and milestone
shape, but add result-choice scoring:

- The prompt must name every fixture path needed for the task.
- `expected_skills` are required primary skills; `optional_skills` are acceptable
  helpers; `forbidden_skills` are common wrong routes.
- The agent must validate paths, run or honestly block analysis, compute key
  metrics or read answer keys, then choose the correct `result_question` option.
- Missing planned files are a blocker, not a reason to fabricate outputs.
- Crop multi-omics and pangenome tasks must keep caveats explicit: ST001947 is
  metabolite-only in the current fixture, and the maize presence/absence table
  is expression-derived ePAV rather than direct assembly-derived PAV truth.

## Source Links

- 10x PBMC3k: https://www.10xgenomics.com/datasets/3-k-pbm-cs-from-a-healthy-donor-1-standard-1-0-0
- Seurat PBMC3k tutorial: https://satijalab.org/seurat/articles/pbmc3k_tutorial
- GEO GSE96583: https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE96583
- cooltools example data docs: https://cooltools.readthedocs.io/en/latest/notebooks/viz.html
- Splatter: https://bioconductor.org/packages/splatter
- scDesign3: https://www.bioconductor.org/packages/release/bioc/html/scDesign3.html
- Polyester: https://bioconductor.org/packages/polyester
- AlphaSimR: https://cran.r-project.org/package=AlphaSimR
- BGLR wheat dataset: https://rdrr.io/cran/BGLR/man/wheat.html
- CLIM4OMICS / G2F Zenodo: https://zenodo.org/records/8002909
- Metabolomics Workbench ST001947: https://www.metabolomicsworkbench.org/data/DRCCMetadata.php?Mode=Study&StudyID=ST001947
- Maize pan-genome Zenodo: https://zenodo.org/records/16576184
- Maize pan-transcriptome Zenodo: https://zenodo.org/records/4994698
- GIAB HG002 GRCh38 v4.2.1: https://ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/release/AshkenazimTrio/HG002_NA24385_son/NISTv4.2.1/GRCh38/
- ONT Methylation Benchmarking Datasets: https://registry.opendata.aws/ont_basemod_data/
- msprime: https://tskit.dev/msprime/docs/stable/intro.html
- SLiM: https://messerlab.org/slim/
- ART: https://www.niehs.nih.gov/research/resources/software/biostatistics/art
- PBSIM3: https://github.com/yukiteruono/pbsim3
- NanoSim: https://github.com/bcgsc/NanoSim
- VarSim: https://bioinform.github.io/varsim/
- ENCODE WGBS data standards: https://www.encodeproject.org/data-standards/wgbs/
