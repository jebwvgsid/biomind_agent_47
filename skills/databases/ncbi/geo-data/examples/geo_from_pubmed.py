"""Find GEO records associated with a PubMed article.

Example:
    python examples/geo_from_pubmed.py 
"""

import argparse
import os
import sys
from Bio import Entrez


def configure_entrez(email: str | None, api_key: str | None) -> None:
    Entrez.email = email or os.environ.get("NCBI_EMAIL") or "your.email@example.com"
    key = api_key or os.environ.get("NCBI_API_KEY")
    if key:
        Entrez.api_key = key


def geo_ids_from_pubmed(pmid: str) -> list[str]:
    handle = Entrez.elink(dbfrom="pubmed", db="gds", id=pmid)
    links = Entrez.read(handle)
    handle.close()
    if not links or not links[0].get("LinkSetDb"):
        return []
    return [item["Id"] for item in links[0]["LinkSetDb"][0]["Link"]]


def fetch_geo_summaries(gds_ids: list[str]):
    if not gds_ids:
        return []
    handle = Entrez.esummary(db="gds", id=",".join(gds_ids))
    summaries = Entrez.read(handle)
    handle.close()
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description="Find GEO datasets linked to a PubMed record.")
    parser.add_argument("--pmid", required=True, help="PubMed ID.")
    parser.add_argument("--email", help="NCBI email. Defaults to NCBI_EMAIL env var.")
    parser.add_argument("--api-key", help="Optional NCBI API key. Defaults to NCBI_API_KEY env var.")
    args = parser.parse_args()

    configure_entrez(args.email, args.api_key)
    gds_ids = geo_ids_from_pubmed(args.pmid)
    if not gds_ids:
        sys.exit(f"No GEO records linked to PMID {args.pmid}")

    summaries = fetch_geo_summaries(gds_ids)
    print(f"PMID {args.pmid}: {len(summaries)} linked GEO records")
    for item in summaries:
        print(f"{item.get('Accession', 'NA')}\t{item.get('n_samples', 'NA')} samples\t{item.get('taxon', 'NA')}")
        print(f"  {item.get('title', '')}")


if __name__ == "__main__":
    main()
