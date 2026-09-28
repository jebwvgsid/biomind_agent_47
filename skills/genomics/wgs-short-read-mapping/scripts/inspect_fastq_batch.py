"""Bounded FASTQ input checks; standard-library CLI, no host protocol or retries."""
from pathlib import Path
import argparse
import json
import math
import subprocess
import sys


def inspect_items(items, evidence_dir, timeout=60.0, options=()):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    if len({row[0] for row in items}) != len(items):
        raise ValueError("each input LABEL must be unique")
    directory = Path(evidence_dir)
    directory.mkdir(parents=True, exist_ok=False)
    rows = []
    for index, (label, path, reader, scope) in enumerate(items, 1):
        argv = [sys.executable, str(Path(__file__).with_name(reader)), "--path", path, *options]
        raw = directory / (str(index) + ".stdout.json")
        err = directory / (str(index) + ".stderr.txt")
        with raw.open("xb") as out, err.open("xb") as stderr:
            try:
                result = subprocess.run(argv, stdout=out, stderr=stderr, stdin=subprocess.DEVNULL,
                                        timeout=timeout, check=False)
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = None
        row = {"label": label, "scope": scope, "returncode": code,
               "raw": {"stdout": str(raw), "stderr": str(err)}, "passed": None}
        if code == 0:
            try:
                value = json.loads(raw.read_text())
                row.update(status="observed", passed=value.get("prefix_valid") is True, finding=value)
            except (ValueError, UnicodeError) as exc:
                row.update(status="error", assessment_error=str(exc))
        else:
            row["status"] = "timeout" if code is None else "error"
        rows.append(row)
        if row["status"] != "observed":
            break
    passed = sum(row["passed"] is True for row in rows)
    value = {"status": "returned" if len(rows) == len(items) and all(r["status"] == "observed" for r in rows) else "partial",
             "total": len(items), "returned": len(rows), "passed": passed,
             "unstarted": len(items) - len(rows), "checks": rows,
             "full_file_validated": False, "all_checks_passed": passed == len(items)}
    (directory / "summary.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return value


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--item", nargs=2, action="append", required=True, metavar=("LABEL", "PATH"))
    p.add_argument("--observation-id", help="Optional legacy result label; output is ordinary JSON")
    p.add_argument("--evidence-dir", required=True, help="New directory under the analysis project")
    p.add_argument("--item-timeout-seconds", type=float, default=60.0)
    p.add_argument("--records", type=int, default=32)
    p.add_argument("--input-bytes", type=int, default=1024 * 1024)
    p.add_argument("--decoded-bytes", type=int, default=65536)
    p.add_argument("--seconds", type=float, default=2.0)
    args = p.parse_args(argv)
    options = [v for k in ("records", "input_bytes", "decoded_bytes", "seconds")
               for v in ("--" + k.replace("_", "-"), str(getattr(args, k)))]
    value = inspect_items([(label, path, "inspect_fastq.py", "fastq_prefix_only") for label, path in args.item],
                          args.evidence_dir, args.item_timeout_seconds, options)
    print(json.dumps(value, ensure_ascii=False), flush=True)
    return 0 if value["all_checks_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
