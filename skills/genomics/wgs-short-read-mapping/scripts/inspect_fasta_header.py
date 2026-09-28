"""Inspect a plain reference FASTA header and one sequence byte within bounds."""
from pathlib import Path
import argparse
import json
import sys

from inspection_readers import inspect_fasta_header, ReadLimits


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, help="Exact approved reference binding; plain FASTA only")
    parser.add_argument("--observation-id", help="Optional result label")
    parser.add_argument("--header-bytes", type=int, default=4096)
    parser.add_argument("--input-bytes", type=int, default=8192)
    parser.add_argument("--seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    try:
        limits = ReadLimits(input_bytes=args.input_bytes, line_bytes=args.header_bytes, seconds=args.seconds)
        with Path(args.path).open("rb") as stream:
            value = inspect_fasta_header(stream, limits=limits)
    except (OSError, ValueError) as exc:
        code = ("READ_PERMISSION_DENIED" if isinstance(exc, PermissionError) else
                "READ_NOT_FOUND" if isinstance(exc, FileNotFoundError) else
                getattr(exc, "code", "READ_IO_ERROR"))
        value = {"status": "error", "error": {"code": code, "type": type(exc).__name__},
                 "scope": "fasta_header_and_first_sequence_byte", "full_file_validated": False}
    if value["status"] == "error":
        print(json.dumps(value, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(value, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
