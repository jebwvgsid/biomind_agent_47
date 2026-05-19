---
name: ncbi-geo-data
description: Discover and inspect public functional genomics datasets in NCBI GEO, including GSE/GSM/GPL/GDS metadata, processed matrices, supplementary files, PubMed links, and GEO-to-SRA run mapping. Use before SRR/ERR/DRR selection; do not use for FASTQ download or RNA-seq processing.
legacy_ids: [bio-geo-data, geo-data, ncbi-geo, geo-search]
---

# NCBI GEO Data

Use this skill to find and inspect public expression datasets in NCBI GEO. It covers GEO Series/DataSets/Samples/Platforms, processed matrix discovery, and GEO-to-SRA linking.

## When To Use

- Find public RNA-seq, microarray, single-cell, ChIP-seq, methylation, or other functional genomics datasets.
- Inspect a `GSE`, `GSM`, `GPL`, or `GDS` accession.
- Find GEO datasets linked to a PubMed paper.
- Check whether a GEO series has linked SRA raw sequencing runs.
- Locate processed series matrix files or supplementary count tables.

Do not use this skill once SRR/ERR/DRR accessions have already been selected for FASTQ download or downstream RNA-seq processing.

## GEO Record Types

| Prefix | Meaning | Typical Use |
|---|---|---|
| `GSE` | Series | Complete study or experiment |
| `GSM` | Sample | One biological/library sample |
| `GPL` | Platform | Array or sequencing platform |
| `GDS` | Curated DataSet | NCBI-curated normalized dataset |

## Expected Inputs

One or more of:

- organism name, e.g. `Saccharum`, `Saccharum officinarum`, `Saccharum spontaneum`, `sugarcane`
- biological condition, e.g. drought, cold, disease, high sugar, sucrose accumulation, stem, leaf
- technology, e.g. RNA-seq, scRNA-seq, ChIP-seq, ATAC-seq, methylation
- GEO accession, e.g. `GSE`, `GSM`, `GPL`, or `GDS`
- PubMed ID
- gene name, trait keyword, tissue, cultivar, treatment, or stress condition

## Expected Outputs

When useful, produce tabular outputs such as:

- `geo_candidates.tsv`: candidate GSE/GDS records
- `geo_samples.tsv`: GSM-level sample metadata
- `geo_platforms.tsv`: GPL/platform metadata
- `geo_supplementary_files.tsv`: processed matrix and supplementary file links
- `geo_sra_runs.tsv`: linked SRR/ERR/DRR runs if available

For each useful dataset, report:

- GEO accession and record type
- title and organism
- sample count
- platform
- study type or technology
- tissue, genotype, treatment, replicate information if available
- PubMed IDs if available
- processed matrix or supplementary file links if available
- linked SRA runs if raw data is needed

## Core Workflow

1. Search GEO DataSets or Series by organism, condition, tissue, gene, technology, or PubMed ID.
2. Summarize candidate datasets with accession, title, sample count, platform, organism, and publication.
3. If raw sequencing is needed, link GEO records to SRA.
4. If processed data is needed, use GEO series matrix files or `GEOparse`.

## Search Patterns

GEO is queried through the Entrez `gds` database.

```python
from Bio import Entrez

Entrez.email = "your.email@example.com"
term = "Saccharum drought RNA-seq AND gse[entry_type]"
handle = Entrez.esearch(db="gds", term=term, retmax=20)
record = Entrez.read(handle)
print(record["Count"], record["IdList"])
```

Useful filters:

| Purpose | Query |
|---|---|
| organism | `Saccharum[orgn]` |
| title keyword | `drought[title]` |
| description keyword | `stress[description]` |
| GEO Series only | `gse[entry_type]` |
| curated GDS only | `gds[entry_type]` |
| high-throughput expression | `expression profiling by high throughput sequencing[gdstype]` |
| publication | `35412348[pubmed]` |
| publication date | `2024[pdat]` |

## Common Tasks

Search GEO series:

```bash
python examples/search_geo.py --term "Saccharum drought RNA-seq" --max-results 10
```

Find SRA runs linked to a GEO series:

```bash
python examples/geo_to_sra.py --gse GSE147507 --out GSE147507_sra_runs.txt
```

Find GEO records linked to a PubMed article:

```bash
python examples/geo_from_pubmed.py --pmid 32228226
```

## Report Back

For each useful dataset, report:

- GEO accession and record type
- title and organism
- sample count
- platform
- study type or technology
- PubMed IDs if available
- FTP/supplementary download link if available
- linked SRA runs if raw data is needed

## Related Skills

- `skills/databases/ncbi/entrez-link` - Link GEO records to SRA and publications.
- `skills/databases/ncbi/entrez-fetch` - Fetch record summaries.
- `skills/databases/id-mapping/gene-id-mapping` - Harmonize gene IDs after expression data retrieval.

## Extended References

- `references/usage-guide.md` - English usage guide.
- `references/usage-guide.zh.md` - 中文用法说明。
- `examples/search_geo.py` - Search GEO series/datasets.
- `examples/geo_to_sra.py` - Link a GSE accession to SRA runs.
- `examples/geo_from_pubmed.py` - Find GEO records associated with a PMID.
