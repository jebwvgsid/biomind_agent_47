#!/usr/bin/env python3
import argparse
from pathlib import Path
import pandas as pd


def main():
    p = argparse.ArgumentParser(description="Summarize crop candidate region popgen statistics with breeding interpretation.")
    p.add_argument("--stats", required=True)
    p.add_argument("--outdir", required=True)
    args = p.parse_args()
    d = pd.read_csv(args.stats, sep=None, engine="python")
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    lines = ["# Candidate Region Population Genetics Summary", ""]
    for _, r in d.iterrows():
        lines.append(f"- {r.get('region', 'region')}: pi={r.get('pi', 'NA')}, TajimaD_approx={r.get('tajimaD_approx', 'NA')}, haplotypes={r.get('haplotype_number', 'NA')}")
    lines.append("")
    lines.append("Interpretation: low pi may indicate reduced diversity; negative Tajima's D may indicate recent positive selection or expansion; validate with population structure.")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote summary -> {out / 'report.md'}")


if __name__ == "__main__":
    main()
