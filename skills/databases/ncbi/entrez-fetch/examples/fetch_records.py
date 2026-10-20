#!/usr/bin/env python
"""Fetch full NCBI records from known IDs/accessions with Bio.Entrez."""

from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from Bio import Entrez


def read_ids(args: argparse.Namespace) -> list[str]:
    ids: list[str] = []
    ids.extend(args.ids or [])
    if args.ids_file:
        ids.extend(Path(args.ids_file).read_text(encoding="utf-8").splitlines())
    return [x.strip() for x in ids if x.strip()]


def configure_entrez(email: str | None, api_key: str | None) -> bool:
    email = email or os.environ.get("NCBI_EMAIL") or getattr(Entrez, "email", None)
    api_key = api_key or os.environ.get("NCBI_API_KEY")
    if not email or email == "your.email@example.com":
        raise SystemExit("Set --email or NCBI_EMAIL before contacting NCBI.")
    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key
    return bool(api_key)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="NCBI database, e.g. nucleotide, protein, pubmed, gene")
    parser.add_argument("--ids", nargs="*", help="IDs or accessions")
    parser.add_argument("--ids-file", help="Text file with one ID/accession per line")
    parser.add_argument("--rettype", help="Return type, e.g. fasta, gb, gp")
    parser.add_argument("--retmode", default="text", help="Return mode, usually text or xml")
    parser.add_argument("--out", required=True, help="Output file")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--email")
    parser.add_argument("--api-key")
    args = parser.parse_args()

    ids = read_ids(args)
    if not ids:
        raise SystemExit("No IDs provided. Use --ids or --ids-file.")

    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    if args.retmode.lower() == "xml" and len(ids) > args.batch_size:
        raise SystemExit("One XML document per --out: use separate output files for separate requests, or an appropriate batch-size for one request. Do not concatenate XML roots.")
    configure_entrez(args.email, args.api_key)
    # Installed Bio.Entrez provides request spacing/retries; bound total time in
    # the existing runner. This helper does not implement another retry layer.
    out_path = Path(args.out)
    failed: list[str] = []

    with out_path.open("wb") as out:
        for start in range(0, len(ids), args.batch_size):
            batch = ids[start : start + args.batch_size]
            kwargs = {
                "db": args.db,
                "id": ",".join(batch),
                "retmode": args.retmode,
            }
            if args.rettype:
                kwargs["rettype"] = args.rettype

            try:
                with Entrez.efetch(**kwargs) as handle:
                    data = handle.read()
                payload = data if isinstance(data, bytes) else data.encode("utf-8")
                if payload.strip():
                    out.write(payload)
                    if args.retmode.lower() == "xml":
                        root = ET.fromstring(payload)
                        if root.tag.lower().endswith("html") or any(node.tag.rsplit("}", 1)[-1].lower() in ("error", "errorlist") for node in root.iter()):
                            raise ValueError("XML payload contains an HTML/API error response")
                    elif not payload.endswith(b"\n"):
                        out.write(b"\n")
                else:
                    failed.extend(batch)
            except Exception as exc:  # noqa: BLE001 - report failed NCBI batches.
                print(f"[WARN] failed batch starting at {start}: {exc}", file=sys.stderr)
                failed.extend(batch)

    failed_path = out_path.with_suffix(out_path.suffix + ".failed_ids.txt")
    failed_path.write_text("\n".join(failed) + ("\n" if failed else ""), encoding="utf-8")
    if failed:
        print(f"Failed IDs: {len(failed)} -> {failed_path}")

    print(f"Requested IDs: {len(ids)}")
    print(f"Output: {out_path}")
    print("Request/payload validation only: record identity and requested-vs-returned IDs need the database-specific parser.")
    return 0 if not failed else (2 if len(failed) < len(ids) else 1)


if __name__ == "__main__":
    raise SystemExit(main())
