---
name: ncbi-entrez-search
description: Search NCBI Entrez databases such as PubMed, Gene, Nucleotide, Protein, SRA, GEO, BioProject, Assembly, and Taxonomy. Use when you need record IDs, result counts, searchable fields, or query translation before fetching or linking records; do not use when you already have final IDs and need full records.
legacy_ids: [bio-entrez-search, entrez-search, ncbi-search, pubmed-search]
---

# NCBI Entrez Search

Use this skill for the discovery step in NCBI databases. It should produce record IDs, counts, query diagnostics, and database field information. It should not download full FASTA/GenBank/PubMed records; use `ncbi-entrez-fetch` after IDs are selected.

## When To Use

- Search PubMed papers by keyword, author, journal, date, MeSH-like terms, or title words.
- Search NCBI Gene, Nucleotide, Protein, SRA, BioProject, Assembly, GEO/GDS, or Taxonomy.
- Count how many records match a query before deciding whether to download.
- Inspect searchable fields and database links with `einfo`.
- Run a broad cross-database count with `egquery`.
- Build reproducible query strings before passing IDs to `entrez-fetch` or `entrez-link`.

Do not use this skill for full record download, GEO-specific metadata parsing, BLAST searches, or Ensembl/UniProt annotations.

## Core Workflow

1. Choose the NCBI database: usually `pubmed`, `gene`, `nucleotide`, `protein`, `sra`, `gds`, `bioproject`, `assembly`, or `taxonomy`.
2. Build an explicit query with field tags when possible.
3. Run `Entrez.esearch()` to get count and IDs.
4. If the field syntax is unclear, run `Entrez.einfo(db=...)`.
5. If the database is unclear, run `Entrez.egquery(term=...)`.
6. Report the database, query string, total count, returned IDs, and whether the query was translated.

## Query Patterns

```python
from Bio import Entrez

Entrez.email = "your.email@example.com"
term = "Saccharum[orgn] AND sucrose synthase[title]"
handle = Entrez.esearch(db="pubmed", term=term, retmax=20)
record = Entrez.read(handle)
handle.close()
print(record["Count"], record["IdList"])
```

Useful databases:

| Database | Use |
|---|---|
| `pubmed` | papers and biomedical literature |
| `gene` | NCBI Gene records |
| `nucleotide` | DNA/RNA sequences |
| `protein` | protein sequences |
| `sra` | sequencing runs/experiments |
| `gds` | GEO DataSets and Series |
| `bioproject` | project-level accessions |
| `assembly` | genome assemblies |
| `taxonomy` | organism taxonomy IDs |

Useful field tags:

| Field | Example |
|---|---|
| organism | `Saccharum[orgn]` |
| gene symbol | `BRCA1[gene]`, `SPS[sym]` |
| accession | `NM_001301717[accn]` |
| title | `drought[title]` |
| author | `Smith J[auth]` |
| journal | `Nature[jour]` |
| publication date | `2024[pdat]` |
| sequence length | `1000:5000[slen]` |
| BioProject | `PRJNA123456[bioproject]` |

## Common Tasks

Search one NCBI database:

```bash
python examples/search_entrez.py --db pubmed --term "Saccharum drought RNA-seq" --max-results 10
```

Inspect database fields and links:

```bash
python examples/database_info.py --db sra
```

Count a term across all NCBI databases:

```bash
python examples/global_query.py --term "Saccharum"
```

## Report Back

For each search, report:

- database searched
- exact query string
- total NCBI count
- number of IDs returned
- returned IDs or accessions
- query translation when NCBI rewrites the query
- recommended next skill, usually `ncbi-entrez-fetch`, `ncbi-entrez-link`, or `ncbi-geo-data`

## Related Skills

- `skills/databases/ncbi/entrez-fetch` - fetch records after IDs are selected.
- `skills/databases/ncbi/entrez-link` - link IDs across NCBI databases.
- `skills/databases/ncbi/geo-data` - GEO-specific dataset discovery and GEO-to-SRA workflows.
- `skills/literature/citations/citation-management` - turn PubMed results into citations/BibTeX.

## Extended References

- `references/usage-guide.md` - English guide.
- `references/usage-guide.zh.md` - 中文用法说明。
- `examples/search_entrez.py` - Search one database.
- `examples/database_info.py` - Inspect database fields and links.
- `examples/global_query.py` - Count a term across all NCBI databases.
