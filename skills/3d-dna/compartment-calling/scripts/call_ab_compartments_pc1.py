#!/usr/bin/env python
"""Call A/B compartments from cooler matrices using PC1/eigenvector analysis."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(label).strip())
    return cleaned.strip("._-") or "sample"


def matrix_base_path(matrix: str) -> str:
    return matrix.split("::", 1)[0]


def parse_resolution_from_uri(matrix: str) -> Optional[int]:
    match = re.search(r"::/resolutions/(\d+)(?:$|/)", matrix)
    return int(match.group(1)) if match else None


def build_matrix_uri(matrix: str, resolution_bp: int) -> str:
    if "::" in matrix:
        return matrix
    if matrix.lower().endswith(".mcool"):
        return f"{matrix}::/resolutions/{resolution_bp}"
    return matrix


def infer_resolution(args: argparse.Namespace) -> int:
    values: List[int] = []
    if args.resolution_bp:
        values.append(args.resolution_bp)
    for _, matrix in args.sample or []:
        matrix_res = parse_resolution_from_uri(matrix)
        if matrix_res:
            values.append(matrix_res)
    if not values:
        raise SystemExit("[ERROR] --resolution-bp is required for .mcool/.cool compartment calling")
    first = values[0]
    if any(value != first for value in values):
        raise SystemExit(f"[ERROR] mixed resolutions are not supported in one run: {values}")
    return first


def write_tsv(path: Path, rows: List[Dict[str, object]], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_phasing_track(path: str):
    import pandas as pd

    cols = ["chrom", "start", "end", "value"]
    track = pd.read_csv(path, sep="\t", comment="#", header=None, usecols=[0, 1, 2, 3], names=cols)
    if track.empty:
        raise SystemExit(f"[ERROR] phasing track is empty: {path}")
    track["start"] = track["start"].astype(int)
    track["end"] = track["end"].astype(int)
    track["value"] = pd.to_numeric(track["value"], errors="coerce")
    track = track.dropna(subset=["value"])
    if track.empty:
        raise SystemExit(f"[ERROR] phasing track has no numeric values in column 4: {path}")
    return track


def import_eigs_cis():
    try:
        from cooltools.api.eigdecomp import eigs_cis  # type: ignore

        return eigs_cis
    except Exception:
        try:
            from cooltools import eigs_cis  # type: ignore

            return eigs_cis
        except Exception as exc:
            raise SystemExit(f"[ERROR] cooltools.eigs_cis is unavailable: {exc}") from exc


def run_eigs_cis(clr, view_df, phasing_track, n_eigs: int):
    eigs_cis = import_eigs_cis()
    kwargs = {"view_df": view_df, "n_eigs": n_eigs}
    if phasing_track is not None:
        kwargs["phasing_track"] = phasing_track
    try:
        return eigs_cis(clr, **kwargs)
    except TypeError:
        # Older cooltools releases use slightly different keyword names.
        kwargs.pop("view_df", None)
        kwargs["view_df"] = view_df
        return eigs_cis(clr, **kwargs)


def extract_eig_tables(result) -> Tuple[object, object]:
    if isinstance(result, tuple) and len(result) >= 2:
        return result[0], result[1]
    if isinstance(result, dict):
        return result.get("cis_eigs") or result.get("eigs"), result.get("eigvecs") or result.get("vectors")
    raise SystemExit("[ERROR] unsupported cooltools.eigs_cis return value")


def eigenvector_column(columns) -> str:
    candidates = ["E1", "eig1", "PC1", "E1_cis"]
    for name in candidates:
        if name in columns:
            return name
    for name in columns:
        if str(name).upper().startswith("E1"):
            return str(name)
    raise SystemExit(f"[ERROR] cannot find first eigenvector column in: {list(columns)}")


def validate_orientation_by_coordinate(eigvecs, phasing_track, sample: str, sample_out: Path) -> List[Dict[str, object]]:
    import pandas as pd

    if phasing_track is None:
        return []

    track = phasing_track[["chrom", "start", "end", "value"]].copy()
    merged = eigvecs[["chrom", "start", "end", "compartment"]].merge(
        track,
        on=["chrom", "start", "end"],
        how="left",
        validate="one_to_one",
    )
    missing = merged["value"].isna().sum()
    if missing:
        raise SystemExit(
            f"[ERROR] phasing track is missing {int(missing)} bin(s) for {sample}; "
            "validate by chrom/start/end, not by row order"
        )

    summary_df = (
        merged.groupby("compartment", dropna=False)["value"]
        .agg(["count", "mean", "sum"])
        .reset_index()
    )
    rows = [
        {
            "sample": sample,
            "compartment": row["compartment"],
            "bins": int(row["count"]),
            "mean_phasing_value": float(row["mean"]),
            "sum_phasing_value": float(row["sum"]),
        }
        for _, row in summary_df.iterrows()
    ]
    write_tsv(
        sample_out / "orientation_validation.tsv",
        rows,
        ["sample", "compartment", "bins", "mean_phasing_value", "sum_phasing_value"],
    )

    means = {str(row["compartment"]): float(row["mean_phasing_value"]) for row in rows}
    if "A" in means and "B" in means and means["A"] <= means["B"]:
        raise SystemExit(
            f"[ERROR] A/B orientation failed for {sample}: "
            f"mean phasing value A={means['A']:.6g} <= B={means['B']:.6g}. "
            "Direction validation must merge by chrom/start/end."
        )
    return rows


def call_sample(sample: str, matrix: str, resolution_bp: int, args: argparse.Namespace) -> List[Dict[str, object]]:
    import cooler
    import pandas as pd

    uri = build_matrix_uri(matrix, resolution_bp)
    clr = cooler.Cooler(uri)
    selected_chroms = args.chromosome or list(clr.chromnames)
    chromsizes = clr.chromsizes
    missing = [chrom for chrom in selected_chroms if chrom not in chromsizes]
    if missing:
        raise SystemExit(f"[ERROR] chromosomes missing from {uri}: {', '.join(missing)}")

    view_df = pd.DataFrame(
        {
            "chrom": selected_chroms,
            "start": [0 for _ in selected_chroms],
            "end": [int(chromsizes[chrom]) for chrom in selected_chroms],
            "name": selected_chroms,
        }
    )
    phasing_track = read_phasing_track(args.phasing_track) if args.phasing_track else None
    _, eigvecs = extract_eig_tables(run_eigs_cis(clr, view_df, phasing_track, args.n_eigs))
    if eigvecs is None:
        raise SystemExit("[ERROR] cooltools.eigs_cis did not return eigenvectors")
    eigvecs = eigvecs.copy()
    pc1_col = eigenvector_column(eigvecs.columns)
    eigvecs = eigvecs[eigvecs["chrom"].isin(selected_chroms)].copy()
    eigvecs["sample"] = sample
    eigvecs["resolution_bp"] = resolution_bp
    eigvecs["score"] = eigvecs[pc1_col]
    eigvecs["compartment"] = eigvecs["score"].apply(lambda value: "A" if float(value) >= 0 else "B")

    sample_out = Path(args.out_root) / str(resolution_bp) / sample
    sample_out.mkdir(parents=True, exist_ok=True)
    validate_orientation_by_coordinate(eigvecs, phasing_track, sample, sample_out)
    eig_fields = ["sample", "chrom", "start", "end", "score", "compartment", "resolution_bp"]
    rows = eigvecs[eig_fields].to_dict("records")
    write_tsv(sample_out / "eigenvectors.tsv", rows, eig_fields)
    with (sample_out / "ab_compartments.bed").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                f"{row['chrom']}\t{int(row['start'])}\t{int(row['end'])}\t"
                f"{row['compartment']}\t{row['score']}\n"
            )
    write_tsv(
        sample_out / "pc1_run_summary.tsv",
        [
            {
                "sample": sample,
                "matrix": matrix,
                "cooler_uri": uri,
                "resolution_bp": resolution_bp,
                "chromosomes": ",".join(selected_chroms),
                "phasing_track": args.phasing_track or "",
                "pc1_column": pc1_col,
                "bins": len(rows),
            }
        ],
        ["sample", "matrix", "cooler_uri", "resolution_bp", "chromosomes", "phasing_track", "pc1_column", "bins"],
    )
    return rows


def summarize_lengths(rows: List[Dict[str, object]], resolution_bp: int) -> List[Dict[str, object]]:
    summary: Dict[Tuple[str, str], Dict[str, object]] = {}
    for row in rows:
        key = (str(row["sample"]), str(row["compartment"]))
        rec = summary.setdefault(
            key,
            {"sample": row["sample"], "resolution_bp": resolution_bp, "compartment": row["compartment"], "count": 0, "total_length": 0},
        )
        rec["count"] = int(rec["count"]) + 1
        rec["total_length"] = int(rec["total_length"]) + int(row["end"]) - int(row["start"])
    return list(summary.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="Call A/B compartments using PC1/eigenvector analysis.")
    parser.add_argument("--sample", nargs=2, action="append", metavar=("LABEL", "MATRIX"), required=True)
    parser.add_argument("--resolution-bp", type=int)
    parser.add_argument("--phasing-track", help="Binned BED track for PC1 sign orientation, e.g. gene density.")
    parser.add_argument("--chromosome", action="append", help="Restrict analysis to one chromosome; repeatable.")
    parser.add_argument("--out-root", default="Compartments/PC1_AB")
    parser.add_argument("--n-eigs", type=int, default=1)
    args = parser.parse_args()

    resolution_bp = infer_resolution(args)
    all_rows: List[Dict[str, object]] = []
    for label, matrix in args.sample:
        if not Path(matrix_base_path(matrix)).is_file():
            raise SystemExit(f"[ERROR] matrix not found: {matrix_base_path(matrix)}")
        all_rows.extend(call_sample(safe_label(label), matrix, resolution_bp, args))

    out_dir = Path(args.out_root) / str(resolution_bp)
    fields = ["sample", "chrom", "start", "end", "score", "compartment", "resolution_bp"]
    write_tsv(out_dir / "compartments_long.tsv", all_rows, fields)
    write_tsv(
        out_dir / "compartment_length_summary.tsv",
        summarize_lengths(all_rows, resolution_bp),
        ["sample", "resolution_bp", "compartment", "count", "total_length"],
    )
    write_tsv(
        out_dir / "compartment_run_summary.tsv",
        [
            {
                "resolution_bp": resolution_bp,
                "samples": ",".join(safe_label(label) for label, _ in args.sample),
                "method": "PC1/eigenvector",
                "phasing_track": args.phasing_track or "",
                "note": "A/B sign is oriented by phasing_track when supplied; otherwise sign may be arbitrary.",
            }
        ],
        ["resolution_bp", "samples", "method", "phasing_track", "note"],
    )
    print(f"[OK] wrote A/B compartment outputs under {out_dir}")


if __name__ == "__main__":
    main()
