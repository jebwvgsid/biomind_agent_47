#!/usr/bin/env python3
import argparse
import gzip
import json
from pathlib import Path
import pandas as pd


def dosage_for_effect(gt, ref, alt, effect_allele):
    gt = gt.split(":")[0].replace("|", "/")
    alleles = gt.split("/")
    if any(a == "." for a in alleles):
        return None, len([a for a in alleles if a != "."])
    allele_map = {"0": ref}
    for i, a in enumerate(alt.split(","), start=1):
        allele_map[str(i)] = a
    called = [allele_map.get(a) for a in alleles]
    return sum(1 for a in called if a == effect_allele), len(called)


def main():
    p = argparse.ArgumentParser(description="Apply crop marker effects to VCF genotypes and compute marker scores.")
    p.add_argument("--vcf", required=True)
    p.add_argument("--marker-effects", required=True)
    p.add_argument("--trait", required=True)
    p.add_argument("--outdir", required=True)
    args = p.parse_args()
    effects = pd.read_csv(args.marker_effects, sep=None, engine="python")
    required = {"chr", "pos", "marker_id", "effect_allele", "effect_weight", "trait"}
    missing = required - set(effects.columns)
    if missing:
        raise SystemExit(f"marker_effects missing columns: {', '.join(sorted(missing))}")
    effects = effects.loc[effects["trait"].astype(str) == args.trait].copy()
    lookup = {(str(r.chr), int(r.pos)): r for r in effects.itertuples(index=False)}
    opener = gzip.open if str(args.vcf).endswith(".gz") else open
    samples = []
    scores = {}
    covered = {r.marker_id: 0 for r in effects.itertuples(index=False)}
    favorable = {}
    ploidy_warnings = set()
    with opener(args.vcf, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("##"):
                continue
            if line.startswith("#CHROM"):
                samples = line.rstrip().split("\t")[9:]
                scores = {s: 0.0 for s in samples}
                favorable = {s: 0 for s in samples}
                continue
            parts = line.rstrip().split("\t")
            key = (parts[0], int(parts[1]))
            if key not in lookup:
                continue
            eff = lookup[key]
            for sample, gt in zip(samples, parts[9:]):
                dose, ploidy = dosage_for_effect(gt, parts[3], parts[4], eff.effect_allele)
                if ploidy != 2:
                    ploidy_warnings.add(f"{parts[0]}:{parts[1]} ploidy={ploidy}")
                if dose is None:
                    continue
                scores[sample] += dose * float(eff.effect_weight)
                if float(eff.effect_weight) > 0:
                    favorable[sample] += dose
                covered[eff.marker_id] += 1
    out = Path(args.outdir); tables = out / "tables"; tables.mkdir(parents=True, exist_ok=True)
    score_df = pd.DataFrame({"sample_id": list(scores), "marker_score": list(scores.values()), "trait": args.trait})
    score_df.sort_values("marker_score", ascending=False).to_csv(tables / "ranked_samples.tsv", sep="\t", index=False)
    score_df.to_csv(tables / "sample_marker_scores.tsv", sep="\t", index=False)
    pd.DataFrame({"sample_id": list(favorable), "favorable_allele_count": list(favorable.values())}).to_csv(tables / "favorable_allele_counts.tsv", sep="\t", index=False)
    cov = pd.DataFrame({"marker_id": list(covered), "called_samples": list(covered.values()), "total_samples": len(samples)})
    cov["coverage"] = cov["called_samples"] / max(len(samples), 1)
    cov.to_csv(tables / "marker_coverage.tsv", sep="\t", index=False)
    warnings = []
    if len(cov) and cov["coverage"].mean() < 0.5:
        warnings.append("Mean marker coverage < 50%.")
    if ploidy_warnings:
        warnings.append("Non-diploid GT detected; review polyploid dosage handling.")
    (out / "result.json").write_text(json.dumps({"warnings": warnings, "n_samples": len(samples), "n_markers": len(effects)}, indent=2), encoding="utf-8")
    print(f"wrote marker scores -> {tables / 'sample_marker_scores.tsv'}")


if __name__ == "__main__":
    main()
