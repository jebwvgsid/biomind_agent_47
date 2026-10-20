---
name: entrez-link
description: Find and inspect relationships between NCBI Entrez databases with ELink, such as Gene to Protein/Nucleotide,
  Gene or Sequence to PubMed, GEO/GDS to SRA, BioProject to BioSample/SRA/Assembly, Assembly to Nucleotide, PubMed
  to related/cited articles, and LinkOut URLs. Use for cross-database navigation, linked UID discovery, linkname
  inspection, and History-server handoff before fetch; do not use for keyword search or fetching final records.
compatibility: 需要本技能 references/dependencies.md 列明的现有软件；不自动安装。输入输出位于分析项目。
---


## 资源定位与依赖

使用当前 Agent 的标准技能发现与文件读取能力加载本文件。`scripts/`、`references/`、
`assets/` 相对本 SKILL.md 的实际目录；输入、输出和样本清单相对本次分析项目。
命令中的 `${SKILL_DIR}` 必须先设为实际技能目录的绝对路径；该变量不由宿主自动提供。
例如，从已加载位置解析目录后，以带引号的脚本绝对路径执行，保持工作目录为分析项目，
不要在技能安装目录写结果。跨技能资源按其实际目录定位；分享完整技能包时保留目录结构。
软件和参考数据库来自已有环境或用户显式提供的位置。依赖见
[dependencies.md](references/dependencies.md)；安装、升级或修改环境需要事先明确授权。
本文命令可由任意提供命令执行能力的 Agent 或终端使用；不会在读取技能时安装或体检环境。

## 联网与结构化结果核验

首次联网前读取 [公共数据库联网与结构化结果核验](../../references/network-and-structured-retrieval.md)，同一会话已读且未变时复用。先确认当前可用的官方 API 或插件路径，再按下列领域工作流执行；复用当前工具 schema 已提供的原始响应模式；搜索摘要和未完整回收的正文不能替代结构化记录。

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

1. Confirm that input values are Entrez UIDs for the source database. Convert accessions or keywords with `entrez-search` first when needed.
2. Choose the source database with `dbfrom` and the destination database with `db`.
3. If the exact link type matters, choose a `linkname`; otherwise start with only `dbfrom` and `db` to discover all links between the databases.
4. Choose the ELink command:
   - `neighbor`: return linked UIDs immediately.
   - `neighbor_history`: post linked UIDs to the History server and return `WebEnv`/`QueryKey` for downstream fetch or summary.
   - `neighbor_score`: return related records with similarity scores, usually same-database relationships such as PubMed related articles.
   - `acheck`: list available link types for the input UIDs.
   - `ncheck`: check whether same-database links exist.
   - `lcheck`, `llinks`, `llinkslib`, `prlinks`: inspect external LinkOut availability or URLs.
5. Parse every returned LinkSet and applicable LinkSetDb by explicit source UID and link type. Preserve one-to-many relationships and source identities. With Bio.Entrez.elink, pass multiple source UIDs as a Python list when per-source mappings are required; a comma-delimited id string merges source sets. Do not assign merged targets back to every source or infer mapping from return order.
6. If final record content is needed, pass linked UIDs or History-server handles to `entrez-fetch`.

## Minimal Python Patterns

Link one Gene UID to RefSeq proteins:

```python
import os
from Bio import Entrez

Entrez.email = os.environ.get("NCBI_EMAIL") or Entrez.email  # Use a real configured contact; never invent one.
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
import os
from Bio import Entrez

Entrez.email = os.environ.get("NCBI_EMAIL") or Entrez.email  # Use a real configured contact; never invent one.
with Entrez.elink(dbfrom="gene", id="672", cmd="acheck") as handle:
    records = Entrez.read(handle)

for linkset in records:
    if linkset.get("ERROR"):
        raise RuntimeError(str(linkset["ERROR"]))
    checks = linkset["IdCheckList"]
    if checks.get("ERROR"):
        raise RuntimeError(str(checks["ERROR"]))
    for id_linkset in checks.get("IdLinkSet", []):
        source_id = str(id_linkset["Id"])
        for link_info in id_linkset.get("LinkInfo", []):
            print(source_id, link_info["DbTo"], link_info["LinkName"], link_info.get("MenuTag", ""))
```

Store linked results on the History server:

```python
import os
from Bio import Entrez

Entrez.email = os.environ.get("NCBI_EMAIL") or Entrez.email  # Use a real configured contact; never invent one.
with Entrez.elink(
    dbfrom="gene",
    db="pubmed",
    id=["672", "7157"],
    linkname="gene_pubmed",
    cmd="neighbor_history",
) as handle:
    records = Entrez.read(handle)

for linkset in records:
    if linkset.get("ERROR"):
        raise RuntimeError(str(linkset["ERROR"]))
    for linkset_db_history in linkset.get("LinkSetDbHistory", []):
        if linkset_db_history.get("DbTo") != "pubmed" or linkset_db_history.get("LinkName") != "gene_pubmed":
            continue
        if linkset_db_history.get("ERROR"):
            raise RuntimeError(str(linkset_db_history["ERROR"]))
        if "QueryKey" not in linkset_db_history:
            print(linkset["IdList"], "History result has no QueryKey:", linkset_db_history.get("Info", "inspect response"))
            continue
        if not linkset.get("WebEnv"):
            raise RuntimeError("History response lacks LinkSet-level WebEnv")
        print(linkset["IdList"], linkset_db_history["DbTo"], linkset_db_history["QueryKey"], linkset["WebEnv"])
```

## Input Handling

- Accept Entrez UIDs as the safest input.
- If the user provides accessions such as `GSE`, `SRR`, `PRJNA`, `NM_`, `NP_`, or assembly accessions, first resolve them to UIDs with `entrez-search`.
- If the user provides PMIDs, use them directly with `dbfrom="pubmed"`.
- If the user provides many IDs, use batches and preserve source-to-linked correspondence. Avoid mixing unrelated source databases in one ELink call.
- Set `Entrez.email`; set `Entrez.api_key` only if the user has provided one through the environment or approved configuration.

## Result Parsing

- `IdList` contains the source UID or UIDs.
- `LinkSetDb` contains immediate linked UIDs grouped by link type.
- `LinkName` identifies the relationship type; report it when present.
- For `neighbor_history`, each `LinkSetDbHistory` holds the target database/link type and a `QueryKey` or diagnostic `Info`; `WebEnv` belongs to its enclosing `LinkSet`. Retain source UIDs with each history set and check `ERROR` instead of fabricating missing handles.
- For `acheck`, detailed links are under `LinkSet -> IdCheckList -> IdLinkSet -> LinkInfo`; `IdLinkSet/Id` identifies the source. An `IdCheckList` can also contain direct `Id` entries; do not mistake an assumed root-level LinkInfo lookup for evidence that no links exist.
- An empty `LinkSetDb` from a validated response means no link was returned by that request. Check API error fields, source UID resolution and the selected link type first; absence of this link does not prove the study has no raw data or publication.
- Multiple `LinkSetDb` blocks can appear when no specific `linkname` is provided; inspect all relevant blocks, not just index 0. Record requested source IDs, returned source IDs and unresolved sources independently.

## Report Back

For each link operation, report:

- source database, destination database, command, and linkname if used
- input IDs and whether they were resolved from accessions
- number of linked records per input ID
- linked IDs grouped by link type
- History-server `WebEnv`/`QueryKey` when generated
- unresolved, invalid, or unlinked input IDs
- recommended next step, usually `entrez-fetch`, `geo-data`, or another targeted database skill

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

