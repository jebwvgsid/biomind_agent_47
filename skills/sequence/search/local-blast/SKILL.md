---
name: local-blast
description: Run BLAST+ locally against custom, private, or downloaded nucleotide/protein databases. Use for local genomes, project-specific annotations, GT21/custom FASTA files, private sequences, reproducible batch searches, and database construction with makeblastdb; do not use for quick public single-query searches when remote BLAST is easier.
legacy_ids: [bio-local-blast, local-blast, blast-plus]
runtime:
  default_env: bio_sequence_search_local_blast_runtime
  env_runner: conda run -n bio_sequence_search_local_blast_runtime
  language: cli
  packages: [blast]
  capabilities: [local-blast, blast-plus, makeblastdb, blastp, blastn, blastdbcmd]
  install_tool: sequence_search_local_blast_runtime
  install_policy: declared-only
  install_command: conda create -y -n bio_sequence_search_local_blast_runtime -c conda-forge -c bioconda blast
  healthcheck: conda run -n bio_sequence_search_local_blast_runtime makeblastdb -version && conda run -n bio_sequence_search_local_blast_runtime blastp -version && conda run -n bio_sequence_search_local_blast_runtime blastn -version && conda run -n bio_sequence_search_local_blast_runtime blastdbcmd -version
---

# Local BLAST Search

Use this skill for reproducible sequence searches against local or custom databases. Prefer local BLAST whenever sequences are private, the target database is project-specific, or the result must be reproducible from a fixed FASTA snapshot.

## When To Use

- Search private or unpublished query sequences.
- Search local genome, transcript, CDS, protein, or annotation FASTA files.
- Search project-specific references such as GT21 gene/protein FASTA files.
- Run many queries or repeated searches where NCBI remote BLAST would be slow or inappropriate.
- Build a frozen searchable database from downloaded NCBI, Ensembl, UniProt, or local FASTA records.

Do not use this skill when the user wants a quick public one-off BLAST with no private data, needs distant homolog detection beyond BLAST sensitivity, or asks for final phylogenetic inference.

## Program Choice

| Query | Database | Program |
|---|---|---|
| nucleotide | nucleotide | `blastn` |
| protein | protein | `blastp` |
| translated nucleotide query | protein | `blastx` |
| protein | translated nucleotide database | `tblastn` |
| translated nucleotide query | translated nucleotide database | `tblastx` |

Use `megablast`-like settings only for very similar nucleotide sequences. Use more sensitive `blastn` settings for divergent nucleotide homologs.

## Core Workflow

1. Verify query FASTA and target FASTA are non-empty and have stable headers.
2. Determine molecule type for query and database.
3. Build or reuse a BLAST database with `makeblastdb`; record the source FASTA, database type, and date.
4. Choose the correct BLAST program and explicit thresholds.
5. Run with explicit `-outfmt`, `-evalue`, `-max_target_seqs`, and `-num_threads`.
6. Parse tabular output, keep all hits needed for coverage/identity filters, and summarize top hits per query.
7. Validate that query IDs, database title, and hit counts match expectations.

## Command Patterns

Build nucleotide and protein databases:

```bash
makeblastdb -in target_genome.fasta -dbtype nucl -parse_seqids -out blastdb/target_genome
makeblastdb -in target_proteins.fasta -dbtype prot -parse_seqids -out blastdb/target_proteins
```

Run a reproducible tabular search:

```bash
blastp \
  -query query_proteins.fasta \
  -db blastdb/target_proteins \
  -out results.tsv \
  -evalue 1e-5 \
  -max_target_seqs 20 \
  -num_threads 8 \
  -outfmt "6 qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore qlen slen qcovs"
```

Useful `outfmt 6` fields:

| Field | Meaning |
|---|---|
| `qseqid`, `sseqid` | query and subject IDs |
| `pident` | percent identity |
| `length` | aligned length |
| `qstart`, `qend`, `sstart`, `send` | alignment coordinates |
| `evalue`, `bitscore` | significance and score |
| `qlen`, `slen` | query and subject lengths |
| `qcovs` | query coverage per subject |

## Filtering Guidance

- For close homologs, start with `evalue <= 1e-10`, `qcovs >= 50`, and inspect identity by gene family.
- For distant homologs, relax e-value only with coverage and domain context; consider `sequence-similarity` for PSI-BLAST/HMMER.
- For short peptides or domains, do not over-rely on percent identity; report alignment length and coverage.
- For duplicated plant gene families, keep multiple hits and avoid calling an ortholog from a single top BLAST hit.
- For reciprocal best hit workflows, run both directions with the same database snapshot and thresholds.

## Expected Outputs

When useful, produce:

- `blast_results.tsv`: raw tabular BLAST output.
- `top_hits.tsv`: best or filtered hits per query.
- `blast_summary.tsv`: query count, hit count, best hit, coverage, identity, e-value, and bitscore.
- `no_hits.txt`: query IDs with no hit under the chosen threshold.

Report the program, query file, database file/prefix, database type, command parameters, thresholds, number of queries, number of queries with hits, and warnings about short alignments or many equally good paralog hits.

## Validation And Troubleshooting

- Run `blastdbcmd -db <db_prefix> -info` to verify database metadata.
- If no hits appear, confirm query/database molecule types and whether `blastx` or `tblastn` is needed.
- If headers cannot be retrieved, rebuild with `-parse_seqids` when FASTA IDs are compatible.
- If output is unexpectedly small, check `-max_target_seqs`, e-value, low-complexity filtering, and sequence orientation.
- Keep BLAST database files together; missing `.pin/.psq/.phr` or `.nin/.nsq/.nhr` components mean the database is incomplete.

## Related Skills

- `skills/sequence/search/remote-blast` - Public NCBI searches for small jobs.
- `skills/sequence/search/sequence-similarity` - More sensitive homology workflows.
- `skills/databases/ncbi/entrez-fetch` - Download sequences for local databases.
- `skills/databases/ensembl/ensembl-database` - Fetch public Ensembl genes, transcripts, proteins, and ortholog context before local searches.
