---
name: ncbi-entrez-fetch
description: Fetch complete NCBI records or compact summaries from known NCBI IDs, accessions, PMID values, or PMID lists using Bio.Entrez, including FASTA, GenBank/GenPept, PubMed abstract/摘要 metadata, Gene summaries, Taxonomy records, and Assembly/SRA/BioSample/BioProject metadata. Use after Entrez search or link has identified IDs; do not use for keyword discovery, GEO dataset exploration, BLAST, or raw FASTQ download.
legacy_ids: [bio-entrez-fetch, entrez-fetch, ncbi-fetch, ncbi-record-fetch]
---

# NCBI Entrez Fetch

Use this skill when the user already has NCBI IDs or accessions and needs record content. This is the retrieval step after `ncbi-entrez-search` or `ncbi-entrez-link`.

## When To Use

- Fetch nucleotide or protein sequences as FASTA.
- Fetch nucleotide GenBank or protein GenPept records for annotation-rich inspection.
- Fetch PubMed records, abstracts, or compact article metadata by PMID.
- Fetch NCBI Gene, Taxonomy, Assembly, SRA, BioSample, or BioProject summaries.
- Save fetched records to local files for downstream analysis.
- Batch-fetch records from an ID list or from an Entrez history result.

Do not use this skill when the user only has keywords, needs GEO-specific dataset discovery, wants BLAST similarity search, or wants raw FASTQ downloads from SRR/ERR/DRR accessions.

## Core Workflow

1. Confirm the IDs/accessions and the target NCBI database.
2. Choose `Entrez.efetch()` for full records or `Entrez.esummary()` for compact metadata.
3. Choose output format: FASTA for sequences, GenBank/GenPept for annotated records, XML/JSON/TSV for metadata.
4. Fetch in batches for long ID lists and respect NCBI rate limits.
5. Save outputs with clear filenames.
6. Validate that files are non-empty and match the requested record type.
7. Report requested count, fetched count, output paths, failed IDs, and warnings.

## Database And Output Choices

| Need | NCBI `db` | Method | Output |
|---|---|---|---|
| DNA/RNA sequence | `nucleotide` | `efetch` | `rettype="fasta"` or `rettype="gb"` |
| Protein sequence | `protein` | `efetch` | `rettype="fasta"` or `rettype="gp"` |
| PubMed article metadata | `pubmed` | `efetch` or `esummary` | XML, TSV summary |
| Gene metadata | `gene` | `esummary` | structured summary |
| Taxonomy record | `taxonomy` | `efetch` | XML |
| Assembly metadata | `assembly` | `esummary` | structured summary |
| SRA metadata | `sra` | `esummary` or `efetch` | XML/summary metadata |
| BioSample/BioProject metadata | `biosample`, `bioproject` | `efetch` or `esummary` | XML/summary metadata |

## Minimal Patterns

Fetch one nucleotide FASTA:

```python
from Bio import Entrez

Entrez.email = "your.email@example.com"
with Entrez.efetch(db="nucleotide", id="NM_007294", rettype="fasta", retmode="text") as handle:
    fasta = handle.read()
```

Fetch one GenBank record:

```python
from Bio import Entrez

Entrez.email = "your.email@example.com"
with Entrez.efetch(db="nucleotide", id="NM_007294", rettype="gb", retmode="text") as handle:
    genbank_text = handle.read()
```

Fetch compact Gene summaries:

```python
from Bio import Entrez

Entrez.email = "your.email@example.com"
with Entrez.esummary(db="gene", id="672") as handle:
    records = Entrez.read(handle)
```

## Common Tasks

Fetch records from known accessions:

```bash
python examples/fetch_records.py --db nucleotide --ids NM_007294 --rettype fasta --retmode text --out NM_007294.fasta
```

Fetch summaries for known IDs:

```bash
python examples/fetch_summaries.py --db gene --ids 672 --out gene_summary.tsv
```

Fetch PubMed metadata and abstracts:

```bash
python examples/fetch_pubmed.py --pmids 32228226 --out pubmed_records.tsv
```

## Validation

- FASTA output should be non-empty and contain headers beginning with `>`.
- GenBank/GenPept output should contain `LOCUS`.
- XML output should be parseable as XML or readable by `Entrez.read()`.
- Summary tables should contain the requested IDs and key metadata fields.
- Failed IDs should be written to a failed-ID file or explicitly reported.

## Report Back

Include:

- database and method used
- requested IDs/accessions count
- successfully fetched records count
- output file paths
- failed or empty IDs
- `rettype` and `retmode`
- important warnings, such as missing abstracts, invalid IDs, or rate-limit retries

## Related Skills

- `skills/databases/ncbi/entrez-search` - find NCBI IDs before fetching.
- `skills/databases/ncbi/entrez-link` - find linked records across NCBI databases before fetching.
- `skills/databases/ncbi/geo-data` - discover and inspect GEO datasets before selecting SRA or processed data.
- `skills/sequence/search/remote-blast` - run NCBI BLAST after sequences are available.
- `skills/sequence/search/local-blast` - build or query local BLAST databases from fetched FASTA.
- `skills/databases/id-mapping/gene-id-mapping` - harmonize gene identifiers after retrieval.

## Extended References

- `references/usage-guide.md` - English user-facing guide with prompt examples.
- `references/usage-guide.zh.md` - 中文用户用法指南和标准提示词。
- `examples/fetch_records.py` - Fetch full text records from IDs/accessions.
- `examples/fetch_summaries.py` - Fetch compact `esummary` metadata.
- `examples/fetch_pubmed.py` - Fetch PubMed title, journal, authors, and abstract.
