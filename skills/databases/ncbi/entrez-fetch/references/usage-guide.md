# NCBI Entrez Fetch - Usage Guide

## Overview

This skill helps you retrieve NCBI record content after you already know the ID or accession. It is for fetching records, not discovering them.

Use it when you have PMIDs, Gene IDs, TaxIDs, nucleotide accessions, protein accessions, Assembly UIDs, SRA UIDs, BioSample UIDs, or BioProject UIDs and want the agent to download or summarize the corresponding records.

If you only have keywords, ask the agent to use Entrez Search first. If you need related IDs across databases, ask the agent to use Entrez Link first.

## Prerequisites

The agent needs Python with Biopython available:

```bash
pip install biopython
```

NCBI also expects an email address for Entrez requests. You can provide it in the prompt or set it as an environment variable:

```bash
set NCBI_EMAIL=your.email@example.com
set NCBI_API_KEY=optional_api_key
```

## Quick Start

Tell your agent what records you already have and what output you want:

- "Fetch FASTA sequences for these nucleotide accessions: NM_007294, NM_007295."
- "Get PubMed titles and abstracts for PMIDs 32228226 and 35412348."
- "Download the GenBank record for NM_007294."
- "Summarize NCBI Gene ID 672."
- "Fetch taxonomy records for TaxIDs 4547 and 4577."

## Example Prompts

### Nucleotide FASTA

> "I have nucleotide accessions NM_007294 and NM_007295. Fetch their FASTA sequences and save them as `brca_sequences.fasta`."

### GenBank Record

> "Fetch the full GenBank record for NM_007294 so I can inspect CDS and feature annotations."

### Protein FASTA

> "Download protein FASTA for NP_009225 and save it as a `.faa` file."

### PubMed Abstracts

> "Get the title, journal, year, authors, and abstract for PMID 32228226."

### Gene Summary

> "Fetch a compact NCBI Gene summary for Gene ID 672, including symbol, description, organism, and functional summary."

### Taxonomy Validation

> "Fetch taxonomy records for TaxIDs 4547 and 4577 and report scientific name, rank, and lineage."

### Batch Fetch

> "Use the IDs in `ids.txt` to fetch nucleotide FASTA records in batches and report any failed IDs."

## What The Agent Will Do

1. Confirm the starting database and ID type.
2. Choose `efetch()` for full records or `esummary()` for compact metadata.
3. Choose the appropriate output format, such as FASTA, GenBank, GenPept, XML, TSV, or JSONL.
4. Set Entrez email/API key if available.
5. Fetch records in batches when the ID list is large.
6. Save outputs with clear filenames.
7. Validate that outputs are non-empty and match the requested record type.
8. Report output files, fetched record counts, failed IDs, and warnings.

## Common Fetch Paths

- Nucleotide accession -> FASTA sequence
- Nucleotide accession -> GenBank annotation record
- Protein accession -> FASTA sequence
- Protein accession -> GenPept annotation record
- PMID -> PubMed title, journal, authors, abstract
- Gene ID -> NCBI Gene summary
- TaxID -> taxonomy name, rank, lineage
- Assembly UID -> assembly summary metadata
- SRA UID -> SRA metadata, not raw FASTQ

## Tips

- Provide the database if you know it, such as `nucleotide`, `protein`, `pubmed`, `gene`, or `taxonomy`.
- Say whether you want full records or summaries.
- For sequences, specify FASTA versus GenBank/GenPept.
- For PubMed, say whether you need abstracts or only citation metadata.
- For long ID lists, provide a file path and ask for batch fetching.
- Remember that this skill retrieves records; it does not search keywords or download raw sequencing FASTQ files.

## Boundaries

- Use `ncbi-entrez-search` first when you only have keywords.
- Use `ncbi-entrez-link` first when you need related records across NCBI databases.
- Use `ncbi-geo-data` for GEO dataset discovery and GEO-to-SRA inspection.
- Use a BLAST skill for similarity search after sequence retrieval.
