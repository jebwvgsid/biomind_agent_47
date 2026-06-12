#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import pandas as pd


def main():
    p = argparse.ArgumentParser(description="Create crop marker score report.")
    p.add_argument("--scores", required=True)
    p.add_argument("--coverage", required=True)
    p.add_argument("--outdir", required=True)
    args = p.parse_args()
    scores = pd.read_csv(args.scores, sep=None, engine="python")
    coverage = pd.read_csv(args.coverage, sep=None, engine="python")
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    mean_cov = float(coverage["coverage"].mean()) if "coverage" in coverage else 0.0
    warning = "WARNING: marker coverage < 50%.\n" if mean_cov < 0.5 else ""
    top = scores.sort_values("marker_score", ascending=False).head(10)
    (out / "report.md").write_text(
        f"# Crop Marker Score Report\n\n{warning}\nMean marker coverage: {mean_cov:.3f}\n\nTop samples:\n\n{top.to_markdown(index=False)}\n",
        encoding="utf-8",
    )
    (out / "result.json").write_text(json.dumps({"mean_marker_coverage": mean_cov, "n_samples": len(scores)}, indent=2), encoding="utf-8")
    print(f"wrote report -> {out / 'report.md'}")


if __name__ == "__main__":
    main()
