# NCBI GEO Data Usage Guide

Use this skill to discover and inspect public expression datasets in NCBI GEO. It is for dataset discovery and metadata navigation, not for full RNA-seq processing.

## When To Use

- Find public RNA-seq, microarray, single-cell, ChIP-seq, or other functional genomics datasets.
- Inspect a `GSE`, `GSM`, `GPL`, or `GDS` accession.
- Find GEO datasets linked to a paper or PubMed ID.
- Check whether a GEO series has linked SRA raw sequencing runs.
- Locate processed matrices or supplementary count files for reuse.

## When Not To Use

- You already have SRR/ERR/DRR accessions and need to download FASTQ.
- You need to run differential expression, alignment, or quantification.
- You need gene coordinates, protein functions, or orthologs.

## GEO Record Types

| Prefix | Meaning | Typical Use |
|---|---|---|
| `GSE` | Series | Whole study or experiment |
| `GSM` | Sample | One biological/library sample |
| `GPL` | Platform | Array or sequencing platform |
| `GDS` | Curated DataSet | NCBI-curated normalized dataset |

## Search Patterns

Use the NCBI `gds` database for GEO discovery.

```python
from Bio import Entrez

Entrez.email = "your.email@example.com"
term = "Saccharum AND RNA-seq AND gse[entry_type]"
handle = Entrez.esearch(db="gds", term=term, retmax=20)
record = Entrez.read(handle)
handle.close()
print(record["Count"], record["IdList"])
```

Useful filters:

| Filter | Example |
|---|---|
| Organism | `Saccharum[orgn]` |
| Title | `drought[title]` |
| Description | `stress[description]` |
| Record type | `gse[entry_type]`, `gds[entry_type]` |
| Study type | `expression profiling by high throughput sequencing[gdstype]` |
| PubMed ID | `35412348[pubmed]` |
| Date | `2024[pdat]` |

## Common Tasks

Search for candidate datasets:

```bash
python examples/search_geo.py --term "Saccharum drought RNA-seq" --max-results 10
```

Find SRA runs linked to a GEO series:

```bash
python examples/geo_to_sra.py --gse GSE147507 --out GSE147507_sra_runs.txt
```

Find GEO datasets linked to a publication:

```bash
python examples/geo_from_pubmed.py --pmid 32228226
```

## Output To Report

For each candidate dataset, report:

- GEO accession
- title
- organism
- sample count
- platform
- study type
- PubMed IDs if available
- FTP or supplementary file link if available
- linked SRA runs if raw sequencing data is needed

## Related Skills

- `ncbi-entrez-search` - general NCBI record discovery.
- `ncbi-entrez-link` - cross-link GEO to SRA or PubMed.
- `ncbi-entrez-fetch` - fetch summaries or metadata records.
- `gene-id-mapping` - map IDs after downloading expression matrices.
