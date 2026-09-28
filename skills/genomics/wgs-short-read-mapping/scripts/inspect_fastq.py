"""Skill action: inspect an explicitly approved FASTQ prefix, never map reads."""
from pathlib import Path
import argparse
import json
import sys

from inspection_readers import inspect_stream, ReadLimits, InspectionReadError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, help="Exact input path from the approved action binding")
    parser.add_argument("--observation-id", help="Optional result label")
    parser.add_argument("--records", type=int, default=32)
    parser.add_argument("--input-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--decoded-bytes", type=int, default=65536)
    parser.add_argument("--seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    try:
        limits = ReadLimits(records=args.records, input_bytes=args.input_bytes,
                            decoded_bytes=args.decoded_bytes, seconds=args.seconds)
        with Path(args.path).open("rb") as stream:
            value = inspect_stream(stream, representation="fastq_prefix", limits=limits)
    except (OSError, ValueError) as exc:
        code = ("READ_PERMISSION_DENIED" if isinstance(exc, PermissionError) else
                "READ_NOT_FOUND" if isinstance(exc, FileNotFoundError) else
                getattr(exc, "code", "READ_IO_ERROR"))
        print(json.dumps({"status": "error", "code": code, "type": type(exc).__name__}), file=sys.stderr)
        return 2
    # A prefix can establish a bounded negative observation (empty/invalid),
    # but technical errors or an incomplete record at a resource limit cannot.
    value.pop("content", None)
    if value["status"] == "error":
        print(json.dumps(value, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(value, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
