"""Approved parent wrapper: bounded prefix checks with separate live progress."""
from pathlib import Path
import argparse
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from execution.preflight import run_checks, with_signal_cleanup
from agent_core.step_effects import emit_observation, STRUCTURED_EFFECT_EVIDENCE_PREFIX


def assess_prefix(row, path):
    """Select the expected nested observation, never forward nested markers."""
    found = []
    with path.open(encoding="utf-8") as handle:
        while True:
            line = handle.readline(65537)
            if not line:
                break
            if len(line) > 65536:
                raise ValueError("PREFIX_CHILD_OUTPUT_LINE_TOO_LARGE")
            if not line.startswith(STRUCTURED_EFFECT_EVIDENCE_PREFIX):
                continue
            payload = json.loads(line[len(STRUCTURED_EFFECT_EVIDENCE_PREFIX):])
            if payload.get("schema_version") != 1:
                raise ValueError("PREFIX_CHILD_EVIDENCE_VERSION_INVALID")
            found.extend(payload.get("produces_observations", []))
    if len(found) != 1 or found[0].get("identity") != row["observation_id"]:
        raise ValueError("PREFIX_CHILD_OBSERVATION_IDENTITY_MISMATCH")
    value = found[0].get("value")
    if not isinstance(value, dict) or value.get("status") != "observed":
        raise ValueError("PREFIX_CHILD_OBSERVATION_INVALID")
    return {"passed": value.get("prefix_valid") is True, "value": value,
            "scope": "fastq_prefix_only"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item", nargs=2, action="append", metavar=("LABEL", "BOUND_PATH"), required=True)
    parser.add_argument("--observation-id", required=True)
    parser.add_argument("--evidence-dir", required=True,
                        help="New directory in the approved execution-log area, not a formal result")
    parser.add_argument("--item-timeout-seconds", type=float,
                        help="Local short-inspection wall deadline; default 60 seconds per item")
    parser.add_argument("--records", type=int, default=32)
    parser.add_argument("--input-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--decoded-bytes", type=int, default=65536)
    parser.add_argument("--seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    child = Path(__file__).with_name("inspect_fastq.py")
    rows = []
    for index, (label, path) in enumerate(args.item, 1):
        identity = f"prefix_item_{index}"
        rows.append({"label": label, "scope": "fastq_prefix_only", "observation_id": identity,
                     "argv": [sys.executable, str(child), "--path", path,
                              "--observation-id", identity, "--records", str(args.records),
                              "--input-bytes", str(args.input_bytes), "--decoded-bytes", str(args.decoded_bytes),
                              "--seconds", str(args.seconds)]})
    try:
        result = asyncio.run(with_signal_cleanup(run_checks(rows, evidence_dir=args.evidence_dir,
            timeout_seconds=args.item_timeout_seconds, assess=assess_prefix)))
    except asyncio.CancelledError:
        return 130
    # One outer marker contains factual findings and UNKNOWN unfinished items.
    # The child's raw marker exists only in its own stdout archive.
    value = {"status": result["status"], "scope": "fastq_prefix_only",
             "full_file_validated": False, "total": result["total"], "returned": result["returned"],
             "passed": result["passed"], "unstarted": result["unstarted"],
             "checks": [{key: row[key] for key in ("label", "status", "passed", "scope", "finding", "raw", "returncode")
                         if key in row} for row in result["checks"]]}
    print(emit_observation(args.observation_id, value), flush=True)
    return 0 if result["status"] == "returned" else 2


if __name__ == "__main__":
    raise SystemExit(main())
