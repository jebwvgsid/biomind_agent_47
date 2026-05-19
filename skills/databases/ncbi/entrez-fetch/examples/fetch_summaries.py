#!/usr/bin/env python
"""Fetch compact NCBI esummary metadata for known IDs."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from Bio import Entrez


DEFAULT_FIELDS = [
    "Id",
    "uid",
    "Title",
    "Name",
    "Description",
    "Accession",
    "Caption",
    "ScientificName",
    "Organism",
    "Summary",
    "PubDate",
    "FullJournalName",
]


def read_ids(args: argparse.Namespace) -> list[str]:
    ids: list[str] = []
    ids.extend(args.ids or [])
    if args.ids_file:
        ids.extend(Path(args.ids_file).read_text(encoding="utf-8").splitlines())
    return [x.strip() for x in ids if x.strip()]


def configure_entrez(email: str | None, api_key: str | None) -> bool:
    email = email or os.environ.get("NCBI_EMAIL")
    api_key = api_key or os.environ.get("NCBI_API_KEY")
    if not email:
        raise SystemExit("Set --email or NCBI_EMAIL before contacting NCBI.")
    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key
    return bool(api_key)


def simplify(value: Any) -> Any:
    if isinstance(value, dict):
        data = {str(k): simplify(v) for k, v in value.items()}
        attrs = getattr(value, "attributes", None)
        if attrs:
            data.update({str(k): simplify(v) for k, v in attrs.items()})
        return data
    if isinstance(value, list):
        return [simplify(v) for v in value]
    return str(value)


def iter_docs(record: Any) -> list[dict[str, Any]]:
    data = simplify(record)
    if isinstance(data, dict) and "DocumentSummarySet" in data:
        docs = data["DocumentSummarySet"].get("DocumentSummary", [])
        return docs if isinstance(docs, list) else [docs]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def field_value(record: dict[str, Any], field: str) -> str:
    if field in record:
        value = record[field]
    elif field == "uid":
        value = record.get("Id", "")
    elif field == "ScientificName" and isinstance(record.get("Organism"), dict):
        value = record["Organism"].get("ScientificName", "")
    else:
        value = ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="NCBI database, e.g. pubmed, gene, assembly, sra")
    parser.add_argument("--ids", nargs="*", help="IDs or accessions")
    parser.add_argument("--ids-file", help="Text file with one ID/accession per line")
    parser.add_argument("--out", required=True, help="Output TSV or JSONL file")
    parser.add_argument("--format", choices=["tsv", "jsonl"], default="tsv")
    parser.add_argument("--fields", help="Comma-separated TSV fields")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--email")
    parser.add_argument("--api-key")
    args = parser.parse_args()

    ids = read_ids(args)
    if not ids:
        raise SystemExit("No IDs provided. Use --ids or --ids-file.")

    has_api_key = configure_entrez(args.email, args.api_key)
    sleep_sec = 0.12 if has_api_key else 0.4
    fields = [x.strip() for x in args.fields.split(",")] if args.fields else DEFAULT_FIELDS
    out_path = Path(args.out)
    failed: list[str] = []
    docs: list[dict[str, Any]] = []

    for start in range(0, len(ids), args.batch_size):
        batch = ids[start : start + args.batch_size]
        try:
            with Entrez.esummary(db=args.db, id=",".join(batch)) as handle:
                record = Entrez.read(handle)
            docs.extend(iter_docs(record))
        except Exception as exc:  # noqa: BLE001 - keep partial results and report IDs.
            print(f"[WARN] failed batch starting at {start}: {exc}", file=sys.stderr)
            failed.extend(batch)
        time.sleep(sleep_sec)

    if args.format == "jsonl":
        with out_path.open("w", encoding="utf-8") as out:
            for doc in docs:
                out.write(json.dumps(doc, ensure_ascii=False) + "\n")
    else:
        with out_path.open("w", newline="", encoding="utf-8") as out:
            writer = csv.writer(out, delimiter="\t")
            writer.writerow(fields)
            for doc in docs:
                writer.writerow([field_value(doc, field) for field in fields])

    if failed:
        failed_path = out_path.with_suffix(out_path.suffix + ".failed_ids.txt")
        failed_path.write_text("\n".join(failed) + "\n", encoding="utf-8")
        print(f"Failed IDs: {len(failed)} -> {failed_path}")

    print(f"Requested IDs: {len(ids)}")
    print(f"Returned summaries: {len(docs)}")
    print(f"Output: {out_path}")
    return 0 if docs else 1


if __name__ == "__main__":
    raise SystemExit(main())
