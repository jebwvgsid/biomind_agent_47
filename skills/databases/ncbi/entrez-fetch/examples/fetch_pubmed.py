#!/usr/bin/env python
"""Fetch PubMed metadata and abstracts from known PMIDs."""

from __future__ import annotations

import argparse
import csv
import os
import time
from pathlib import Path
from typing import Any

from Bio import Entrez


def read_pmids(args: argparse.Namespace) -> list[str]:
    pmids: list[str] = []
    pmids.extend(args.pmids or [])
    if args.pmids_file:
        pmids.extend(Path(args.pmids_file).read_text(encoding="utf-8").splitlines())
    return [x.strip() for x in pmids if x.strip()]


def configure_entrez(email: str | None, api_key: str | None) -> bool:
    email = email or os.environ.get("NCBI_EMAIL")
    api_key = api_key or os.environ.get("NCBI_API_KEY")
    if not email:
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
        "Title": as_text(article_data.get("ArticleTitle")),
        "Journal": as_text(journal.get("Title")),
        "Year": as_text(pub_date.get("Year") or pub_date.get("MedlineDate")),
        "Authors": "; ".join(authors),
        "Abstract": as_text(abstract_parts),
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

    has_api_key = configure_entrez(args.email, args.api_key)
    sleep_sec = 0.12 if has_api_key else 0.4
    rows: list[dict[str, str]] = []

    for start in range(0, len(pmids), args.batch_size):
        batch = pmids[start : start + args.batch_size]
        with Entrez.efetch(db="pubmed", id=",".join(batch), retmode="xml") as handle:
            record = Entrez.read(handle)
        rows.extend(parse_article(article) for article in record.get("PubmedArticle", []))
        time.sleep(sleep_sec)

    fields = ["PMID", "Title", "Journal", "Year", "Authors", "Abstract"]
    with Path(args.out).open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Requested PMIDs: {len(pmids)}")
    print(f"Returned articles: {len(rows)}")
    print(f"Output: {args.out}")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
