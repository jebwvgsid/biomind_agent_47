"""Search one NCBI Entrez database and print IDs/counts.

Example:
    python examples/search_entrez.py --db pubmed --term "Saccharum drought RNA-seq" --max-results 10
"""

import argparse
import os
from Bio import Entrez


def configure_entrez(email: str | None, api_key: str | None) -> None:
    Entrez.email = email or os.environ.get("NCBI_EMAIL") or "your.email@example.com"
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

    configure_entrez(args.email, args.api_key)
    kwargs = {"db": args.db, "term": args.term, "retmax": args.max_results}
    if args.sort:
        kwargs["sort"] = args.sort

    handle = Entrez.esearch(**kwargs)
    record = Entrez.read(handle)
    handle.close()

    print(f"Database: {args.db}")
    print(f"Query: {args.term}")
    print(f"Total matches: {record.get('Count', '0')}")
    print(f"Returned IDs: {len(record.get('IdList', []))}")
    for item in record.get("IdList", []):
        print(item)
    if record.get("QueryTranslation"):
        print(f"QueryTranslation: {record['QueryTranslation']}")


if __name__ == "__main__":
    main()
