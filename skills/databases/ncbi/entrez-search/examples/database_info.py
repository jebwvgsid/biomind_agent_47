"""Inspect searchable fields and database links for an NCBI Entrez database.

Example:
    python examples/database_info.py --db sra
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
    parser = argparse.ArgumentParser(description="Show Entrez database fields and links.")
    parser.add_argument("--db", help="NCBI database. Omit to list available databases.")
    parser.add_argument("--email", help="NCBI email. Defaults to NCBI_EMAIL env var.")
    parser.add_argument("--api-key", help="Optional NCBI API key. Defaults to NCBI_API_KEY env var.")
    args = parser.parse_args()

    configure_entrez(args.email, args.api_key)
    if not args.db:
        handle = Entrez.einfo()
        record = Entrez.read(handle)
        handle.close()
        print("Available databases:")
        for db in record.get("DbList", []):
            print(db)
        return

    handle = Entrez.einfo(db=args.db)
    record = Entrez.read(handle)
    handle.close()
    info = record["DbInfo"]
    print(f"Database: {info.get('DbName', args.db)}")
    print(f"Description: {info.get('Description', '')}")
    print(f"Count: {info.get('Count', 'NA')}")
    print(f"Last update: {info.get('LastUpdate', 'NA')}")
    print("\nFields:")
    for field in info.get("FieldList", []):
        print(f"{field.get('Name', '')}\t{field.get('FullName', '')}\t{field.get('Description', '')}")
    print("\nLinks:")
    for link in info.get("LinkList", []):
        print(f"{link.get('Name', '')}\t-> {link.get('DbTo', '')}\t{link.get('Description', '')}")


if __name__ == "__main__":
    main()
