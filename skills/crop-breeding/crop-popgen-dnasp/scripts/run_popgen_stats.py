#!/usr/bin/env python3
import argparse
import json
import math
from itertools import combinations
from pathlib import Path
import pandas as pd


def read_fasta(path):
    seqs = {}
    name = None
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            name = line[1:].split()[0]
            seqs[name] = []
        else:
            seqs[name].append(line.upper())
    return {k: "".join(v) for k, v in seqs.items()}


def pairwise_diffs(seqs):
    vals = list(seqs.values())
    ncomp = 0
    diffs = 0
    for a, b in combinations(vals, 2):
        for x, y in zip(a, b):
            if x in "N-" or y in "N-":
                continue
            ncomp += 1
            diffs += x != y
    return diffs, ncomp


def stats_for(seqs):
    n = len(seqs)
    length = min(map(len, seqs.values())) if seqs else 0
    seg = 0
    for i in range(length):
        alleles = {s[i] for s in seqs.values() if s[i] not in "N-"}
        if len(alleles) > 1:
            seg += 1
    diffs, comps = pairwise_diffs(seqs)
    pi = diffs / comps if comps else 0
    a1 = sum(1 / i for i in range(1, n)) if n > 1 else 0
    theta = seg / a1 / length if a1 and length else 0
    tajd = pi - theta
    haps = pd.Series(list(seqs.values())).value_counts()
    hd = (n / (n - 1)) * (1 - sum((c / n) ** 2 for c in haps)) if n > 1 else 0
    return {"n_samples": n, "alignment_length": length, "segregating_sites": seg, "pi": pi, "theta_w": theta, "tajimaD_approx": tajd, "haplotype_number": len(haps), "haplotype_diversity": hd}


def main():
    p = argparse.ArgumentParser(description="Compute DnaSP-like crop population genetics stats from FASTA alignment.")
    p.add_argument("--alignment", required=True)
    p.add_argument("--sample-group")
    p.add_argument("--outdir", required=True)
    p.add_argument("--window-size", type=int, default=0)
    p.add_argument("--step-size", type=int, default=0)
    args = p.parse_args()
    seqs = read_fasta(args.alignment)
    out = Path(args.outdir); tables = out / "tables"; tables.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"region": Path(args.alignment).stem, **stats_for(seqs)}]).to_csv(tables / "polymorphism_stats.tsv", sep="\t", index=False)
    if args.sample_group:
        groups = pd.read_csv(args.sample_group, sep=None, engine="python")
        rows = []
        for group, sub in groups.groupby("group"):
            subset = {s: seqs[s] for s in sub["sample_id"] if s in seqs}
            if subset:
                rows.append({"group": group, **stats_for(subset)})
        pd.DataFrame(rows).to_csv(tables / "pi_by_group.tsv", sep="\t", index=False)
    pd.DataFrame([{"region": Path(args.alignment).stem, "tajimaD": stats_for(seqs)["tajimaD_approx"]}]).to_csv(tables / "tajimaD_by_region.tsv", sep="\t", index=False)
    (out / "result.json").write_text(json.dumps(stats_for(seqs), indent=2), encoding="utf-8")
    (out / "report.md").write_text("# Crop PopGen DnaSP Report\n\nLow pi may indicate reduced diversity; Tajima's D should be interpreted with population structure.\n", encoding="utf-8")
    print(f"wrote popgen stats -> {tables}")


if __name__ == "__main__":
    main()
