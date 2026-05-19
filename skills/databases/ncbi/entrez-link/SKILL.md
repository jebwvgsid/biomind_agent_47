---
name: ncbi-entrez-link
description: Find and inspect relationships between NCBI Entrez databases with ELink, such as Gene to Protein/Nucleotide, Gene or Sequence to PubMed, GEO/GDS to SRA, BioProject to BioSample/SRA/Assembly, Assembly to Nucleotide, PubMed to related/cited articles, and LinkOut URLs. Use for cross-database navigation, linked UID discovery, linkname inspection, and History-server handoff before fetch; do not use for keyword search or fetching final records.
legacy_ids: [bio-entrez-link, entrez-link, ncbi-elink, ncbi-crossref]
---

# NCBI Entrez Link

Use this skill when the main task is navigation between NCBI databases rather than keyword discovery or record download. ELink starts from known Entrez UIDs in a source database and returns linked UIDs, related records, link availability, History-server handles, or external LinkOut URLs.

## When To Use

- Map known NCBI Gene IDs to linked Protein, Nucleotide, PubMed, HomoloGene-like, or pathway-related records.
- Map sequence records to Gene, Protein, Assembly, BioProject, BioSample, SRA, or PubMed records.
- Map GEO DataSets or Series records in `gds` to SRA runs or PubMed publications.
- Map BioProject accessions to SRA, BioSample, Assembly, or Nucleotide records after resolving the BioProject UID.
- Find related PubMed articles, cited-by/citing relationships, or PMC full-text links.
- Inspect available link types with `cmd="acheck"` or database metadata before choosing a precise link.
- Store linked result sets on the NCBI History server with `cmd="neighbor_history"` for later `esummary()` or `efetch()`.

Do not use this skill when the user only has keywords, wants full FASTA/GenBank/PubMed records, needs raw FASTQ download, or needs GEO-specific processed matrix parsing.

## Common Link Paths

| Task | `dbfrom` | `db` | Typical `linkname` |
|---|---|---|---|
| Gene to RefSeq proteins | `gene` | `protein` | `gene_protein_refseq` |
| Gene to nucleotide transcripts/genomic records | `gene` | `nucleotide` | inspect with `acheck`; often `gene_nuccore_refseqrna` or related |
| Gene to publications | `gene` | `pubmed` | `gene_pubmed` |
| Protein to source gene | `protein` | `gene` | `protein_gene` |
| Nucleotide to gene | `nucleotide` | `gene` | `nuccore_gene` |
| GEO/GDS to SRA | `gds` | `sra` | inspect with `acheck`; use available `gds_sra`-style link |
| GEO/GDS to PubMed | `gds` | `pubmed` | inspect with `acheck`; often `gds_pubmed` |
| BioProject to SRA | `bioproject` | `sra` | inspect with `acheck` |
| BioProject to BioSample | `bioproject` | `biosample` | inspect with `acheck` |
| Assembly to Nucleotide | `assembly` | `nucleotide` | inspect with `acheck` |
| PubMed related articles | `pubmed` | `pubmed` | `pubmed_pubmed` |
| PubMed to PMC full text | `pubmed` | `pmc` | `pubmed_pmc` |

Link names can change or be database-specific. If the linkname is uncertain, first run `Entrez.einfo(db=source_db)` to inspect available links or run `Entrez.elink(cmd="acheck", dbfrom=source_db, id=...)` to see links present for specific IDs.

## Core Workflow

1. Confirm that input values are Entrez UIDs for the source database. Convert accessions or keywords with `ncbi-entrez-search` first when needed.
2. Choose the source database with `dbfrom` and the destination database with `db`.
3. If the exact link type matters, choose a `linkname`; otherwise start with only `dbfrom` and `db` to discover all links between the databases.
4. Choose the ELink command:
   - `neighbor`: return linked UIDs immediately.
   - `neighbor_history`: post linked UIDs to the History server and return `WebEnv`/`QueryKey` for downstream fetch or summary.
   - `neighbor_score`: return related records with similarity scores, usually same-database relationships such as PubMed related articles.
   - `acheck`: list available link types for the input UIDs.
   - `ncheck`: check whether same-database links exist.
   - `lcheck`, `llinks`, `llinkslib`, `prlinks`: inspect external LinkOut availability or URLs.
5. Parse results by input UID. Preserve one-to-many mappings; do not collapse linked records without reporting counts.
6. If final record content is needed, pass linked UIDs or History-server handles to `ncbi-entrez-fetch`.

## Minimal Python Patterns

Link one Gene UID to RefSeq proteins:

```python
from Bio import Entrez

Entrez.email = "your.email@example.com"
with Entrez.elink(
    dbfrom="gene",
    db="protein",
    id="672",
    linkname="gene_protein_refseq",
) as handle:
    records = Entrez.read(handle)

for linkset in records:
    source_id = linkset["IdList"][0]
    linked = []
    for linkset_db in linkset.get("LinkSetDb", []):
        linked.extend(link["Id"] for link in linkset_db.get("Link", []))
    print(source_id, linked)
```

Inspect available links for a source UID:

```python
from Bio import Entrez

Entrez.email = "your.email@example.com"
with Entrez.elink(dbfrom="gene", id="672", cmd="acheck") as handle:
    records = Entrez.read(handle)

for link_info in records[0].get("LinkInfo", []):
    print(link_info.get("DbTo"), link_info.get("LinkName"), link_info.get("MenuTag"))
```

Store linked results on the History server:

```python
from Bio import Entrez

Entrez.email = "your.email@example.com"
with Entrez.elink(
    dbfrom="gene",
    db="pubmed",
    id=["672", "7157"],
    linkname="gene_pubmed",
    cmd="neighbor_history",
) as handle:
    records = Entrez.read(handle)

for linkset in records:
    for linkset_db_history in linkset.get("LinkSetDbHistory", []):
        print(linkset_db_history["DbTo"], linkset_db_history["QueryKey"], linkset_db_history["WebEnv"])
```

## Input Handling

- Accept Entrez UIDs as the safest input.
- If the user provides accessions such as `GSE`, `SRR`, `PRJNA`, `NM_`, `NP_`, or assembly accessions, first resolve them to UIDs with `ncbi-entrez-search`.
- If the user provides PMIDs, use them directly with `dbfrom="pubmed"`.
- If the user provides many IDs, use batches and preserve source-to-linked correspondence. Avoid mixing unrelated source databases in one ELink call.
- Set `Entrez.email`; set `Entrez.api_key` only if the user has provided one through the environment or approved configuration.

## Result Parsing

- `IdList` contains the source UID or UIDs.
- `LinkSetDb` contains immediate linked UIDs grouped by link type.
- `LinkName` identifies the relationship type; report it when present.
- `LinkSetDbHistory` contains `WebEnv` and `QueryKey` from `neighbor_history`.
- Empty `LinkSetDb` usually means no link exists for that UID/linkname, not necessarily that the source record is invalid.
- Multiple `LinkSetDb` blocks can appear when no specific `linkname` is provided.

## Report Back

For each link operation, report:

- source database, destination database, command, and linkname if used
- input IDs and whether they were resolved from accessions
- number of linked records per input ID
- linked IDs grouped by link type
- History-server `WebEnv`/`QueryKey` when generated
- unresolved, invalid, or unlinked input IDs
- recommended next step, usually `ncbi-entrez-fetch`, `ncbi-geo-data`, or another targeted database skill

## Practical Notes

- Respect NCBI rate limits; batch requests and retry transient HTTP errors conservatively.
- Prefer `neighbor_history` for large linked result sets to avoid transferring huge UID lists and to make downstream `efetch`/`esummary` cleaner.
- Use `retmode="xml"` or BioPython `Entrez.read()` for structured parsing.
- Record the query date, database names, linkname, command, and source IDs for reproducibility.
- Do not assume accessions and Entrez UIDs are interchangeable; ELink operates on UIDs.

## Related Skills

- `skills/databases/ncbi/entrez-search` - Find source records.
- `skills/databases/ncbi/entrez-fetch` - Retrieve linked records.
- `skills/databases/ncbi/geo-data` - GEO-specific discovery and GEO-to-SRA workflows.

