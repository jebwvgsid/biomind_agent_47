"""Find SRA runs linked to a GEO Series accession.

Example:
    python examples/geo_to_sra.py --gse GSE147507 --out GSE147507_sra_runs.txt
"""

import argparse
import csv
import os
import sys
from Bio import Entrez


def configure_entrez(email: str | None, api_key: str | None) -> None:
    Entrez.email = email or os.environ.get("NCBI_EMAIL") or "your.email@example.com"
    key = api_key or os.environ.get("NCBI_API_KEY")
    if key:
        Entrez.api_key = key


def find_geo_id(gse: str) -> str | None:
    handle = Entrez.esearch(db="gds", term=f"{gse}[accn]")
    record = Entrez.read(handle)
    handle.close()
    return record["IdList"][0] if record["IdList"] else None


def linked_sra_ids(gds_id: str) -> list[str]:
    handle = Entrez.elink(dbfrom="gds", db="sra", id=gds_id)
    links = Entrez.read(handle)
    handle.close()
    if not links or not links[0].get("LinkSetDb"):
        return []
    return [item["Id"] for item in links[0]["LinkSetDb"][0]["Link"]]


def fetch_runinfo(sra_ids: list[str]) -> list[dict[str, str]]:
    if not sra_ids:
        return []
    handle = Entrez.efetch(db="sra", id=",".join(sra_ids[:200]), rettype="runinfo", retmode="text")
    text = handle.read()
    handle.close()
    return list(csv.DictReader(text.splitlines()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Map a GEO Series accession to SRA runs.")
    parser.add_argument("--gse", required=True, help="GEO Series accession, e.g. GSE147507.")
    parser.add_argument("--out", help="Optional output text file containing one run accession per line.")
    parser.add_argument("--email", help="NCBI email. Defaults to NCBI_EMAIL env var.")
    parser.add_argument("--api-key", help="Optional NCBI API key. Defaults to NCBI_API_KEY env var.")
    args = parser.parse_args()

    configure_entrez(args.email, args.api_key)
    gds_id = find_geo_id(args.gse)
    if not gds_id:
        sys.exit(f"No GEO record found for {args.gse}")

    sra_ids = linked_sra_ids(gds_id)
    runs = fetch_runinfo(sra_ids)
    run_accessions = [row["Run"] for row in runs if row.get("Run")]
    print(f"{args.gse}: {len(run_accessions)} SRA runs")
    for row in runs[:20]:
        print(f"{row.get('Run', 'NA')}\t{row.get('LibraryStrategy', 'NA')}\t{row.get('spots', 'NA')} spots")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            for run in run_accessions:
                handle.write(run + "\n")
        print(f"Wrote {len(run_accessions)} runs to {args.out}")


if __name__ == "__main__":
    main()
