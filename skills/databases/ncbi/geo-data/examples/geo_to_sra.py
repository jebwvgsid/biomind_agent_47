"""Find SRA runs linked to a GEO Series accession.

Example:
    python examples/geo_to_sra.py --gse GSE147507 --out GSE147507_sra_runs.txt
"""

import argparse
import csv
import io
import json
import hashlib
from pathlib import Path
import tempfile
import re
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


def find_geo_id(gse: str) -> str | None:
    if not re.fullmatch(r"GSE[0-9]+", gse):
        raise ValueError("Expected one GSE accession")
    with Entrez.esearch(db="gds", term=f"{gse}[Accession] AND gse[entry_type]", retmax=2) as handle:
        record = Entrez.read(handle)
    if "Count" not in record or "IdList" not in record:
        raise RuntimeError("Malformed ESearch response")
    if int(record["Count"]) == 0:
        return None
    if int(record["Count"]) != 1 or len(record["IdList"]) != 1:
        raise RuntimeError("GSE accession did not resolve to exactly one Series UID")
    gds_id = str(record["IdList"][0])
    with Entrez.esummary(db="gds", id=gds_id) as handle:
        summaries = Entrez.read(handle)
    if not isinstance(summaries, list) or len(summaries) != 1 or str(summaries[0].get("Id", "")) != gds_id or str(summaries[0].get("Accession", "")) != gse:
        raise RuntimeError("Resolved GDS UID does not match the requested GSE accession")
    return gds_id


def linked_sra_ids(gds_id: str) -> list[str]:
    handle = Entrez.elink(dbfrom="gds", db="sra", id=gds_id)
    links = Entrez.read(handle)
    handle.close()
    if not links:
        raise RuntimeError("ELink returned no LinkSet; this is not a verified empty link result")
    linked_ids = []
    for linkset in links:
        source_ids = [str(x) for x in linkset.get("IdList", [])]
        if linkset.get("DbFrom") != "gds" or source_ids != [gds_id]:
            raise RuntimeError("ELink response did not preserve the requested source UID")
        if linkset.get("ERROR"):
            raise RuntimeError(str(linkset["ERROR"]))
        for block in linkset.get("LinkSetDb", []):
            if block.get("ERROR"):
                raise RuntimeError(str(block["ERROR"]))
            if str(block.get("DbTo", "")) != "sra":
                continue
            linked_ids.extend(str(item["Id"]) for item in block.get("Link", []))
    return list(dict.fromkeys(linked_ids))


def fetch_runinfo(sra_ids: list[str], raw_dir: Path | None = None):
    """Query every selected UID; CSV alone does not establish per-UID identity."""
    rows_by_run = {}
    selected = list(dict.fromkeys(str(uid) for uid in sra_ids))
    report = {"selected_uids": selected, "queried_uids": [], "failed_uids": [],
              "batches": [], "uid_to_run_identity": "not_provided_by_runinfo_csv"}
    for start in range(0, len(selected), 200):
        batch = selected[start:start + 200]
        row_report = {"requested_uids": batch, "status": "pending"}
        report["batches"].append(row_report)
        report["queried_uids"].extend(batch)
        try:
            with Entrez.efetch(db="sra", id=",".join(batch), rettype="runinfo", retmode="text") as handle:
                raw = handle.read()
            payload = raw if isinstance(raw, bytes) else raw.encode("utf-8")
            row_report["payload_sha256"] = hashlib.sha256(payload).hexdigest()
            row_report["payload_representation"] = "returned_bytes" if isinstance(raw, bytes) else "handle_text_encoded_utf8"
            if raw_dir is not None:
                raw_path = raw_dir / f"batch-{len(report['batches']):04d}.csv"
                raw_path.write_bytes(payload)
                row_report["raw_path"] = str(raw_path)
            text = payload.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text, newline=""))
            if not reader.fieldnames or "Run" not in reader.fieldnames:
                raise RuntimeError("Expected runinfo CSV with a Run column")
            batch_rows = list(reader)
            if not batch_rows:
                raise RuntimeError("No run rows returned; source-UID coverage remains unverified")
            pending = {}
            for row in batch_rows:
                run = row.get("Run", "")
                if not re.fullmatch(r"[SED]RR[0-9]+", run):
                    raise RuntimeError("Invalid Run accession in runinfo CSV")
                existing = pending.get(run, rows_by_run.get(run))
                if existing is not None and existing != row:
                    raise RuntimeError("Conflicting rows for the same Run accession")
                pending[run] = row
            rows_by_run.update(pending)
            row_report.update(status="csv_parsed_uid_identity_unverified", returned_rows=len(batch_rows), unique_runs=len(pending))
        except Exception as exc:
            row_report.update(status="failed_or_unverified", error_type=type(exc).__name__)
            report["failed_uids"].extend(batch)
            print(f"Batch {len(report['batches'])}: {type(exc).__name__}; inspect saved payload and retrieval receipt", file=sys.stderr)
    report["unqueried_uids"] = sorted(set(selected) - set(report["queried_uids"]))
    report["all_selected_uids_queried"] = not report["unqueried_uids"]
    report["status"] = "partial" if report["failed_uids"] else "queried_all_record_identity_unverified"
    return list(rows_by_run.values()), report


def main() -> int:
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
    raw_dir = Path(tempfile.mkdtemp(prefix=Path(args.out).name + ".raw-", dir=Path(args.out).parent)) if args.out else None
    runs, coverage = fetch_runinfo(sra_ids, raw_dir)
    run_accessions = [row["Run"] for row in runs if row.get("Run")]
    print(f"{args.gse}: {len(sra_ids)} selected SRA UIDs; {len(coverage['queried_uids'])} queried; {len(coverage['failed_uids'])} failed/unverified; {len(run_accessions)} unique Runs")
    print("runinfo CSV does not prove each UID-to-Run mapping; this helper does not replace required SRA XML.")
    if not run_accessions:
        print("No runs returned by this request; this is not proof the study has no raw data", file=sys.stderr)
    for row in runs[:20]:
        print(f"{row.get('Run', 'NA')}\t{row.get('LibraryStrategy', 'NA')}\t{row.get('spots', 'NA')} spots")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            for run in run_accessions:
                handle.write(run + "\n")
        Path(args.out + ".retrieval.json").write_text(json.dumps(coverage, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {len(run_accessions)} runs to {args.out}; raw batches and coverage receipt retained")
    else:
        print(json.dumps(coverage, separators=(",", ":")))
    return 2 if coverage["failed_uids"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
