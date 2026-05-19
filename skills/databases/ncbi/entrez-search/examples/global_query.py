"""Count a query across NCBI Entrez databases.

Example:
    python examples/global_query.py --term "Saccharum"
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
    parser = argparse.ArgumentParser(description="Run Entrez egquery across NCBI databases.")
    parser.add_argument("--term", required=True, help="Search term.")
    parser.add_argument("--email", help="NCBI email. Defaults to NCBI_EMAIL env var.")
    parser.add_argument("--api-key", help="Optional NCBI API key. Defaults to NCBI_API_KEY env var.")
    args = parser.parse_args()

    configure_entrez(args.email, args.api_key)
    handle = Entrez.egquery(term=args.term)
    record = Entrez.read(handle)
    handle.close()

    rows = []
    for item in record.get("eGQueryResult", []):
        try:
            count = int(item.get("Count", "0"))
        except ValueError:
            count = 0
        if count > 0:
            rows.append((item.get("DbName", ""), count))

    for db, count in sorted(rows, key=lambda x: (-x[1], x[0])):
        print(f"{db}\t{count}")


if __name__ == "__main__":
    main()
