"""Bounded FASTQ + plain FASTA header inspection; ordinary JSON and project logs."""
import argparse
import json
from inspect_fastq_batch import inspect_items


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fastq", nargs=2, action="append", required=True, metavar=("LABEL", "PATH"))
    p.add_argument("--reference", nargs=2, required=True, metavar=("LABEL", "PATH"))
    p.add_argument("--observation-id", help="Optional legacy result label; output is ordinary JSON")
    p.add_argument("--evidence-dir", required=True, help="New directory under the analysis project")
    p.add_argument("--item-timeout-seconds", type=float, default=60.0)
    args = p.parse_args(argv)
    items = [(label, path, "inspect_fastq.py", "fastq_prefix_only") for label, path in args.fastq]
    items.append((*args.reference, "inspect_fasta_header.py", "fasta_header_and_first_sequence_byte"))
    value = inspect_items(items, args.evidence_dir, args.item_timeout_seconds)
    value["scope"] = "fastq_prefix_and_fasta_header"
    print(json.dumps(value, ensure_ascii=False), flush=True)
    return 0 if value["all_checks_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
