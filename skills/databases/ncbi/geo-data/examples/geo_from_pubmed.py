"""Find GEO records associated with a PubMed article.

Example:
    python examples/geo_from_pubmed.py 
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


def geo_ids_from_pubmed(pmid: str) -> list[str]:
    handle = Entrez.elink(dbfrom="pubmed", db="gds", id=pmid)
    links = Entrez.read(handle)
    handle.close()
    if not links:
        raise RuntimeError("ELink returned no source LinkSet; empty links are not verified")
    ids = []
    for linkset in links:
        if linkset.get("ERROR") or linkset.get("DbFrom") != "pubmed" or [str(x) for x in linkset.get("IdList", [])] != [pmid]:
            raise RuntimeError("ELink error or source PMID mismatch")
        for block in linkset.get("LinkSetDb", []):
            if block.get("ERROR"):
                raise RuntimeError(str(block["ERROR"]))
            if block.get("DbTo") == "gds":
                ids.extend(str(item["Id"]) for item in block.get("Link", []))
    return list(dict.fromkeys(ids))


def fetch_geo_summaries(gds_ids: list[str]):
    if not gds_ids:
        return []
    selected = list(dict.fromkeys(gds_ids))
    summaries = []
    for start in range(0, len(selected), 200):
        batch = selected[start:start + 200]
        with Entrez.esummary(db="gds", id=",".join(batch)) as handle:
            records = Entrez.read(handle)
        if not isinstance(records, list):
            raise RuntimeError("Expected default GDS ESummary DocSum list")
        returned = [str(item.get("Id", "")) for item in records]
        if len(returned) != len(set(returned)) or set(returned) != set(batch):
            raise RuntimeError("GDS summary UID coverage does not match the requested batch")
        summaries.extend(records)
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
        print(f"No GDS links returned for PMID {args.pmid} by this verified ELink request; this does not prove the publication has no dataset.")
        return

    summaries = fetch_geo_summaries(gds_ids)
    print(f"PMID {args.pmid}: {len(summaries)} linked GEO records")
    for item in summaries:
        print(f"{item.get('Accession', 'NA')}\t{item.get('n_samples', 'NA')} samples\t{item.get('taxon', 'NA')}")
        print(f"  {item.get('title', '')}")


if __name__ == "__main__":
    main()
