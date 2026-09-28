#!/usr/bin/env python3
"""Generate one LDBlockShow job per GWAS candidate window."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows", required=True, type=Path, help="BED with chr start end")
    parser.add_argument("--vcf", required=True)
    parser.add_argument("--gwas", required=True)
    parser.add_argument("--gff")
    parser.add_argument("--outdir", required=True, type=Path)
    parser.add_argument("--cutline", default="6")
    parser.add_argument("--script", required=True, type=Path)
    args = parser.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)
    args.script.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
    for raw in args.windows.read_text().splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        fields = raw.split()
        if len(fields) < 3:
            raise SystemExit(f"malformed BED row: {raw}")
        chrom, start, end = fields[:3]
        prefix = args.outdir / f"{chrom}.{start}-{end}.ldblock"
        lines.extend([
            f"LDBlockShow -InVCF {args.vcf} -Region {chrom}:{start}-{end} -OutPut {prefix} -InGWAS {args.gwas} -OutPng",
            f"ShowLDSVG -InPreFix {prefix} -InGWAS {args.gwas} -Cutline {args.cutline} -OutPng -OutPut {prefix}.addline",
        ])
        if args.gff:
            lines.append(
                f"ShowLDSVG -InPreFix {prefix} -InGWAS {args.gwas} -Cutline {args.cutline} -InGFF {args.gff} -OutPng -OutPut {prefix}.addline.addgene"
            )
        lines.append("")
    args.script.write_text("\n".join(lines) + "\n")
    args.script.chmod(0o755)


if __name__ == "__main__":
    main()
