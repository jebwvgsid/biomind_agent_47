"""Explicit FASTQ + plain reference FASTA preflight; one complete observation.

This is a small composition of the two readers, not extension-based dispatch.
The helper alone creates the nonexisting evidence leaf from the approved binding.
"""
from pathlib import Path
import argparse
import asyncio
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from execution.preflight import run_checks, with_signal_cleanup
from agent_core.step_effects import emit_observation
from inspect_fastq_batch import assess_prefix


def assess_input(row, path):
    finding = assess_prefix(row, path)
    expected = "fastq_prefix" if row["scope"] == "fastq_prefix_only" else "fasta_header"
    if finding["value"].get("operation") != expected:
        raise ValueError("PREFLIGHT_READER_TYPE_MISMATCH")
    finding["scope"] = row["scope"]
    return finding


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fastq", nargs=2, action="append", required=True, metavar=("LABEL", "BOUND_PATH"))
    parser.add_argument("--reference", nargs=2, required=True, metavar=("LABEL", "BOUND_PATH"))
    parser.add_argument("--observation-id", required=True)
    parser.add_argument("--evidence-dir", required=True,
                        help="Approved runtime leaf: must NOT exist; do not mkdir or reuse it")
    parser.add_argument("--item-timeout-seconds", type=float)
    args = parser.parse_args(argv)
    items = [(label, path, "fastq_prefix_only", "inspect_fastq.py") for label, path in args.fastq]
    items.append((*args.reference, "fasta_header_and_first_sequence_byte", "inspect_fasta_header.py"))
    if len({item[0] for item in items}) != len(items):
        parser.error("each input LABEL must be unique")
    rows = [{"label": label, "scope": scope, "observation_id": f"input_{index}",
             "argv": [sys.executable, str(Path(__file__).with_name(reader)), "--path", path,
                      "--observation-id", f"input_{index}"]}
            for index, (label, path, scope, reader) in enumerate(items, 1)]
    try:
        result = asyncio.run(with_signal_cleanup(run_checks(rows, evidence_dir=args.evidence_dir,
            timeout_seconds=args.item_timeout_seconds, assess=assess_input)))
    except asyncio.CancelledError:
        return 130
    value = {key: result[key] for key in ("status", "total", "returned", "passed", "unstarted")}
    value.update(scope="fastq_prefix_and_fasta_header", full_file_validated=False,
        checks=[{key: row[key] for key in ("label", "status", "passed", "scope", "finding", "raw", "returncode", "assessment_error")
                 if key in row} for row in result["checks"]],
        unstarted_checks=[{"label": row["label"], "scope": row["scope"], "status": "unstarted", "passed": None}
                          for row in rows[len(result["checks"]):]])
    # Technical return and semantic observation are separate. Preserve every
    # actual finding; a negative or incomplete result cannot look successful.
    value["all_checks_passed"] = result["status"] == "returned" and result["passed"] == result["total"]
    print(emit_observation(args.observation_id, value), flush=True)
    return 0 if value["all_checks_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
