"""Search one NCBI Entrez database and print IDs/counts.

Example:
    python examples/search_entrez.py --db pubmed --term "Saccharum drought RNA-seq" --max-results 10
"""

import argparse
import os
import sys
from Bio import Entrez


def configure_entrez(email: str | None, api_key: str | None) -> None:
    resolved_email = email or os.environ.get("NCBI_EMAIL") or getattr(Entrez, "email", None)
    if resolved_email == "your.email@example.com":
        resolved_email = None
    Entrez.email = resolved_email
    if not resolved_email:
        print("[WARN] No real NCBI contact email configured; no placeholder is sent. Configure --email or NCBI_EMAIL for NCBI contact/usage guidance. This is not an authentication failure.", file=sys.stderr)
    key = api_key or os.environ.get("NCBI_API_KEY")
    if key:
        Entrez.api_key = key


def main() -> None:
    parser = argparse.ArgumentParser(description="Search an NCBI Entrez database.")
    parser.add_argument("--db", required=True, help="NCBI database, e.g. pubmed, gene, nucleotide, protein, sra, gds.")
    parser.add_argument("--term", required=True, help="Entrez search term.")
    parser.add_argument("--max-results", type=int, default=20)
    parser.add_argument("--sort", help="Optional database-specific sort value.")
    parser.add_argument("--email", help="NCBI email. Defaults to NCBI_EMAIL env var.")
    parser.add_argument("--api-key", help="Optional NCBI API key. Defaults to NCBI_API_KEY env var.")
    args = parser.parse_args()

    if args.max_results <= 0:
        raise SystemExit("--max-results must be positive")
    configure_entrez(args.email, args.api_key)
    kwargs = {"db": args.db, "term": args.term, "retmax": args.max_results}
    if args.sort:
        kwargs["sort"] = args.sort

    handle = Entrez.esearch(**kwargs)
    record = Entrez.read(handle)
    handle.close()

    if "Count" not in record or "IdList" not in record:
        raise RuntimeError("Malformed ESearch response: Count or IdList missing")
    if record.get("ERROR") or record.get("ErrorList"):
        raise RuntimeError("ESearch returned an API error; inspect the response")
    count = int(record["Count"])
    ids = [str(uid) for uid in record["IdList"]]
    if count < 0 or len(ids) != len(set(ids)) or len(ids) > count:
        raise RuntimeError("Inconsistent ESearch counts or duplicate IDs")
    print(f"Database: {args.db}")
    print(f"Query: {args.term}")
    print(f"Total matches: {record['Count']}")
    print(f"Returned IDs: {len(record['IdList'])}")
    print(f"Retmax: {args.max_results}; status: {'complete_id_list' if len(ids) == count else 'partial_id_list'}")
    for item in record.get("IdList", []):
        print(item)
    if record.get("QueryTranslation"):
        print(f"QueryTranslation: {record['QueryTranslation']}")


if __name__ == "__main__":
    main()
