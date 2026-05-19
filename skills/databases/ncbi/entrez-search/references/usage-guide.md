# NCBI Entrez Search Usage Guide

This skill is the entry point for discovering records in NCBI databases. Use it to find IDs and understand database structure before fetching records or linking across databases.

## Main Entrez Functions

| Function | Purpose |
|---|---|
| `esearch` | Search one database and return IDs/counts |
| `einfo` | Show available databases, fields, and links |
| `egquery` | Count a term across many NCBI databases |

## Typical User Requests

- "Search PubMed for sugarcane drought RNA-seq papers."
- "Find NCBI Gene records for sucrose synthase in Saccharum."
- "How many SRA records mention sugarcane drought?"
- "What fields can I use in the SRA database?"
- "Find nucleotide accessions for a specific organism and gene."

## Search Strategy

1. Pick the smallest useful database.
2. Add organism and field filters.
3. Use `retmax` to limit returned IDs.
4. Check `Count` to see whether the query is too broad.
5. Check `QueryTranslation` for unexpected NCBI rewriting.
6. Use `entrez-fetch` only after the desired IDs are selected.

## Field Examples

| Goal | Query |
|---|---|
| PubMed topic | `Saccharum drought RNA-seq` |
| Species-specific records | `Saccharum[orgn]` |
| Gene symbol | `SPS[sym]` or `BRCA1[gene]` |
| Nucleotide accession | `NM_001301717[accn]` |
| Recent papers | `2024[pdat]` |
| SRA RNA-seq | `RNA-Seq[Strategy]` or plain `RNA-seq` with SRA search |
| Sequence length | `1000:5000[slen]` |

## Output Checklist

Return a compact table with:

- database
- query
- total count
- returned IDs
- query translation
- suggested next action

## Next Skills

- `ncbi-entrez-fetch`: retrieve full records, summaries, FASTA, XML, or PubMed metadata.
- `ncbi-entrez-link`: traverse related records, such as PubMed to GEO or Gene to Protein.
- `ncbi-geo-data`: inspect GEO datasets after a GEO/GDS candidate is found.
