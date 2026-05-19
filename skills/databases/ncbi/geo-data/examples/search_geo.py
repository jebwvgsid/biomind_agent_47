"""Search NCBI GEO series or curated datasets.

Example:
    python examples/search_geo.py --term "Saccharum drought RNA-seq" --max-results 10
"""

import argparse
import os
from Bio import Entrez


def configure_entrez(email: str | None, api_key: str | None) -> None:
    Entrez.email = email or os.environ.get("NCBI_EMAIL") or "your.email@example.com"
    key = api_key or os.environ.get("NCBI_API_KEY")
    if key:
        Entrez.api_key = key


def search_geo(term: str, entry_type: str, max_results: int):
    full_term = f"{term} AND {entry_type}[entry_type]"
    handle = Entrez.esearch(db="gds", term=full_term, retmax=max_results)
    search = Entrez.read(handle)
    handle.close()
    if not search["IdList"]:
        return search["Count"], []

    handle = Entrez.esummary(db="gds", id=",".join(search["IdList"]))
    summaries = Entrez.read(handle)
    handle.close()
    return search["Count"], summaries


def main() -> None:
    parser = argparse.ArgumentParser(description="Search NCBI GEO records.")
    parser.add_argument("--term", required=True, help="Entrez query terms, e.g. 'Saccharum drought RNA-seq'.")
    parser.add_argument("--entry-type", default="gse", choices=["gse", "gds"], help="GEO record type to return.")
    parser.add_argument("--max-results", type=int, default=20)
    parser.add_argument("--email", help="NCBI email. Defaults to NCBI_EMAIL env var.")
    parser.add_argument("--api-key", help="Optional NCBI API key. Defaults to NCBI_API_KEY env var.")
    args = parser.parse_args()

    configure_entrez(args.email, args.api_key)
    total, summaries = search_geo(args.term, args.entry_type, args.max_results)
    print(f"Total matches: {total}")
    for item in summaries:
        print(f"{item.get('Accession', 'NA')}\t{item.get('n_samples', 'NA')} samples\t{item.get('taxon', 'NA')}")
        print(f"  {item.get('title', '')}")
        print(f"  Platform: {item.get('GPL', 'NA')} | Type: {item.get('gdsType', 'NA')}")


if __name__ == "__main__":
    main()
