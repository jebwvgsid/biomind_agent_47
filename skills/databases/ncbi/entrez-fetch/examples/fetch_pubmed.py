#!/usr/bin/env python
"""Fetch PubMed metadata and abstracts from known PMIDs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import tempfile
import os
from pathlib import Path
from typing import Any

from Bio import Entrez


def read_pmids(args: argparse.Namespace) -> list[str]:
    pmids: list[str] = []
    pmids.extend(args.pmids or [])
    if args.pmids_file:
        pmids.extend(Path(args.pmids_file).read_text(encoding="utf-8").splitlines())
    return list(dict.fromkeys(x.strip() for x in pmids if x.strip()))


def configure_entrez(email: str | None, api_key: str | None) -> bool:
    email = email or os.environ.get("NCBI_EMAIL") or getattr(Entrez, "email", None)
    api_key = api_key or os.environ.get("NCBI_API_KEY")
    if not email or email == "your.email@example.com":
        raise SystemExit("Set --email or NCBI_EMAIL before contacting NCBI.")
    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key
    return bool(api_key)


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(as_text(x) for x in value if x is not None)
    return str(value)


def parse_article(article: dict[str, Any]) -> dict[str, str]:
    citation = article.get("MedlineCitation", {})
    pmid = as_text(citation.get("PMID"))
    article_data = citation.get("Article", {})
    journal = article_data.get("Journal", {})
    journal_issue = journal.get("JournalIssue", {})
    pub_date = journal_issue.get("PubDate", {})
    abstract_parts = article_data.get("Abstract", {}).get("AbstractText", [])
    authors = []
    for author in article_data.get("AuthorList", []):
        last = author.get("LastName", "")
        initials = author.get("Initials", "")
        collective = author.get("CollectiveName", "")
        name = collective or " ".join(x for x in [last, initials] if x)
        if name:
            authors.append(name)
    return {
        "PMID": pmid,
        "RecordType": "PubmedArticle",
        "Title": as_text(article_data.get("ArticleTitle")),
        "Journal": as_text(journal.get("Title")),
        "Year": as_text(pub_date.get("Year") or pub_date.get("MedlineDate")),
        "Authors": "; ".join(authors),
        "Abstract": as_text(abstract_parts),
    }


def parse_book_article(article: dict[str, Any]) -> dict[str, str]:
    document = article.get("BookDocument", {})
    book = document.get("Book", {})
    date = document.get("ContributionDate") or book.get("PubDate", {})
    # BookDocument permits repeated AuthorList groups; do not use book editors
    # as chapter authors. Accept parser groups and a direct author list.
    names = []
    for group in document.get("AuthorList", []):
        if getattr(group, "attributes", {}).get("Type") == "editors":
            continue
        authors = [group] if isinstance(group, dict) else group
        for author in authors:
            if not isinstance(author, dict):
                raise RuntimeError("Unexpected BookDocument AuthorList shape")
            name = author.get("CollectiveName") or " ".join(str(x) for x in [author.get("LastName", ""), author.get("Initials", "")] if x)
            if name:
                names.append(str(name))
    return {
        "PMID": as_text(document.get("PMID")), "RecordType": "PubmedBookArticle",
        "Title": as_text(document.get("ArticleTitle") or book.get("BookTitle")),
        "Journal": as_text(book.get("BookTitle")),
        "Year": as_text(date.get("Year") or date.get("MedlineDate")),
        "Authors": "; ".join(names),
        "Abstract": as_text(document.get("Abstract", {}).get("AbstractText", [])),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pmids", nargs="*", help="PubMed IDs")
    parser.add_argument("--pmids-file", help="Text file with one PMID per line")
    parser.add_argument("--out", required=True, help="Output TSV")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--email")
    parser.add_argument("--api-key")
    args = parser.parse_args()

    pmids = read_pmids(args)
    if not pmids:
        raise SystemExit("No PMIDs provided. Use --pmids or --pmids-file.")

    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    configure_entrez(args.email, args.api_key)
    # Bio.Entrez handles request spacing and retries; the existing runner bounds total time.
    rows: list[dict[str, str]] = []
    raw_dir = Path(tempfile.mkdtemp(prefix=Path(args.out).name + ".raw-", dir=Path(args.out).parent))
    receipts = []
    duplicate_ids = []
    for start in range(0, len(pmids), args.batch_size):
        batch = pmids[start:start + args.batch_size]
        receipt = {"requested_pmids": batch, "status": "pending"}
        receipts.append(receipt)
        try:
            with Entrez.efetch(db="pubmed", id=",".join(batch), retmode="xml") as handle:
                raw = handle.read()
            payload = raw if isinstance(raw, bytes) else raw.encode("utf-8")
            raw_path = raw_dir / f"batch-{len(receipts):04d}.xml"
            raw_path.write_bytes(payload)
            receipt.update(raw_path=str(raw_path), sha256=hashlib.sha256(payload).hexdigest(),
                           payload_representation="returned_bytes" if isinstance(raw, bytes) else "handle_text_encoded_utf8")
            record = Entrez.read(io.BytesIO(payload))
            if not isinstance(record, dict) or not any(key in record for key in ("PubmedArticle", "PubmedBookArticle")) or record.get("ERROR"):
                raise RuntimeError("Expected PubmedArticleSet without an API error")
            batch_rows = [parse_article(x) for x in record.get("PubmedArticle", [])]
            batch_rows.extend(parse_book_article(x) for x in record.get("PubmedBookArticle", []))
            by_pmid = {}
            for row in batch_rows:
                pmid = row["PMID"]
                if not pmid or pmid not in batch:
                    raise RuntimeError("PubMed record PMID is missing or was not requested in this batch")
                if pmid in by_pmid:
                    duplicate_ids.append(pmid)
                    if by_pmid[pmid] != row:
                        raise RuntimeError("Conflicting records for the same PMID")
                by_pmid[pmid] = row
            rows.extend(by_pmid.values())
            receipt.update(status="parsed", returned_pmids=list(by_pmid), missing_pmids=sorted(set(batch)-set(by_pmid)))
        except Exception as exc:
            receipt.update(status="failed_or_unverified", error_type=type(exc).__name__)

    requested_ids = set(pmids)
    returned_ids = {row["PMID"] for row in rows if row["PMID"]}
    missing_ids = sorted(requested_ids - returned_ids)
    unexpected_ids = sorted(returned_ids - requested_ids)
    if unexpected_ids:
        raise RuntimeError("Fetched PubMed records contain IDs that were not requested")
    failed_path = Path(str(args.out) + ".missing_pmids.txt")
    failed_path.write_text("\n".join(missing_ids) + ("\n" if missing_ids else ""), encoding="utf-8")
    if missing_ids:
        print(f"Missing or unverified PMIDs: {len(missing_ids)}; see {failed_path}")
    receipt_path = Path(str(args.out) + ".retrieval.json")
    receipt_path.write_text(json.dumps({"requested_pmids": pmids, "returned_pmids": sorted(returned_ids), "missing_pmids": missing_ids, "duplicate_pmids": sorted(set(duplicate_ids)), "batches": receipts}, indent=2) + "\n", encoding="utf-8")
    fields = ["PMID", "Title", "Journal", "Year", "Authors", "Abstract", "RecordType"]
    with Path(args.out).open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Requested PMIDs: {len(pmids)}; unique requested IDs: {len(requested_ids)}")
    print(f"Returned records: {len(rows)}")
    print(f"Output: {args.out}")
    return 0 if rows and not missing_ids else (2 if rows else 1)


if __name__ == "__main__":
    raise SystemExit(main())
