#!/usr/bin/env python3
"""Validate enrichment inputs and write deterministic QC tables."""

import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path


MISSING = {"", "na", "nan", "none", "null", "-", "."}


def sniff_delimiter(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    sample = "\n".join([line for line in text.splitlines() if line.strip() and not line.startswith("#")][:5])
    if "\t" in sample:
        return "\t"
    if "," in sample:
        return ","
    return None


def read_rows(path):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    if path.stat().st_size == 0:
        raise ValueError(f"{path} is empty")
    delim = sniff_delimiter(path)
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        lines = [line for line in handle if line.strip() and not line.startswith("#")]
    if not lines:
        raise ValueError(f"{path} has no data rows")
    if delim is None:
        return [{"V1": line.strip().split()[0]} for line in lines if line.strip().split()]
    reader = csv.DictReader(lines, delimiter=delim)
    if reader.fieldnames and any(name and not re.match(r"^[A-Za-z0-9_.:-]+$", name) for name in reader.fieldnames):
        pass
    rows = list(reader)
    if not rows:
        return []
    return rows


def choose_column(rows, requested, label):
    if not rows:
        raise ValueError(f"{label} has no rows")
    cols = list(rows[0].keys())
    if requested and requested in cols:
        return requested
    lower = {c.lower(): c for c in cols}
    if requested and requested.lower() in lower:
        return lower[requested.lower()]
    for candidate in ("gene_id", "gene", "id", "GeneID", "Gene", "transcript_id", "protein_id", "V1"):
        if candidate in cols:
            return candidate
        if candidate.lower() in lower:
            return lower[candidate.lower()]
    return cols[0]


def read_ids(path, column, label):
    rows = read_rows(path)
    col = choose_column(rows, column, label)
    values = []
    missing = 0
    for row in rows:
        value = str(row.get(col, "")).strip()
        if value.lower() in MISSING:
            missing += 1
            continue
        values.append(value)
    counts = Counter(values)
    unique = sorted(counts)
    duplicates = sorted([gene for gene, count in counts.items() if count > 1])
    return {
        "path": str(path),
        "column": col,
        "raw_count": len(values),
        "missing_count": missing,
        "unique_ids": unique,
        "duplicates": duplicates,
        "duplicate_counts": counts,
    }


def read_term2gene(path, term_column, gene_column):
    rows = read_rows(path)
    if not rows:
        raise ValueError("TERM2GENE has no rows")
    cols = list(rows[0].keys())
    lower = {c.lower(): c for c in cols}
    tcol = term_column if term_column in cols else lower.get(term_column.lower(), None)
    gcol = gene_column if gene_column in cols else lower.get(gene_column.lower(), None)
    if tcol is None:
        tcol = lower.get("term_id") or lower.get("term") or cols[0]
    if gcol is None:
        gcol = lower.get("gene_id") or lower.get("gene") or (cols[1] if len(cols) > 1 else cols[0])
    term_to_genes = defaultdict(set)
    gene_to_terms = defaultdict(set)
    for row in rows:
        term = str(row.get(tcol, "")).strip()
        gene = str(row.get(gcol, "")).strip()
        if term.lower() in MISSING or gene.lower() in MISSING:
            continue
        term_to_genes[term].add(gene)
        gene_to_terms[gene].add(term)
    return {
        "term_column": tcol,
        "gene_column": gcol,
        "term_to_genes": term_to_genes,
        "gene_to_terms": gene_to_terms,
    }


def write_tsv(path, rows, header):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in header})


def add_qc(rows, metric, value, status="pass", detail=""):
    rows.append({"metric": metric, "value": value, "status": status, "detail": detail})


def numeric_id_status(ids, gene_id_type):
    if not ids:
        return "pass", ""
    numeric_count = sum(1 for item in ids if re.fullmatch(r"\d+", item))
    if numeric_count != len(ids):
        return "pass", f"{numeric_count}/{len(ids)} IDs are numeric"
    expected = any(token in gene_id_type.lower() for token in ("entrez", "ncbi", "numeric"))
    if expected:
        return "pass", "all IDs are numeric and gene ID type indicates this is expected"
    return "warning", "all IDs are numeric; confirm this is not a lost gene-symbol column"


def main():
    parser = argparse.ArgumentParser(description="Check gene set enrichment inputs and write QC outputs.")
    parser.add_argument("--foreground", required=True, help="Foreground gene list or table.")
    parser.add_argument("--foreground-column", default="gene_id")
    parser.add_argument("--background", help="Background gene universe list or table.")
    parser.add_argument("--background-column", default="gene_id")
    parser.add_argument("--term2gene", help="TERM2GENE table with term_id and gene_id columns.")
    parser.add_argument("--term-column", default="term_id")
    parser.add_argument("--gene-column", default="gene_id")
    parser.add_argument("--species", default="")
    parser.add_argument("--gene-id-type", default="")
    parser.add_argument("--analysis-type", default="")
    parser.add_argument("--database-name", default="")
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--warn-mapping-rate", type=float, default=0.70)
    parser.add_argument("--min-mapping-rate", type=float, default=0.40)
    parser.add_argument("--require-background", action="store_true")
    parser.add_argument("--require-term2gene", action="store_true")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    blocked = []
    warnings = []
    qc = []

    fg = read_ids(args.foreground, args.foreground_column, "foreground")
    fg_set = set(fg["unique_ids"])
    add_qc(qc, "species", args.species or "missing", "pass" if args.species else "blocked", "")
    add_qc(qc, "gene_id_type", args.gene_id_type or "missing", "pass" if args.gene_id_type else "blocked", "")
    add_qc(qc, "analysis_type", args.analysis_type or "not_provided")
    add_qc(qc, "foreground_path", fg["path"])
    add_qc(qc, "foreground_column", fg["column"])
    add_qc(qc, "foreground_raw_count", fg["raw_count"])
    add_qc(qc, "foreground_unique_count", len(fg_set))
    add_qc(qc, "foreground_missing_count", fg["missing_count"], "warning" if fg["missing_count"] else "pass")
    add_qc(qc, "foreground_duplicate_count", len(fg["duplicates"]), "warning" if fg["duplicates"] else "pass")
    num_status, num_detail = numeric_id_status(fg["unique_ids"], args.gene_id_type)
    add_qc(qc, "foreground_numeric_id_check", num_detail or "not_all_numeric", num_status)

    if not args.species:
        blocked.append("Species is missing.")
    if not args.gene_id_type:
        blocked.append("Gene ID type is missing.")
    if not fg_set:
        blocked.append("Foreground gene list has no usable IDs.")

    bg_set = None
    if args.background:
        bg = read_ids(args.background, args.background_column, "background")
        bg_set = set(bg["unique_ids"])
        add_qc(qc, "background_path", bg["path"])
        add_qc(qc, "background_column", bg["column"])
        add_qc(qc, "background_raw_count", bg["raw_count"])
        add_qc(qc, "background_unique_count", len(bg_set))
        add_qc(qc, "background_missing_count", bg["missing_count"], "warning" if bg["missing_count"] else "pass")
        add_qc(qc, "background_duplicate_count", len(bg["duplicates"]), "warning" if bg["duplicates"] else "pass")
        bg_num_status, bg_num_detail = numeric_id_status(bg["unique_ids"], args.gene_id_type)
        add_qc(qc, "background_numeric_id_check", bg_num_detail or "not_all_numeric", bg_num_status)
        overlap = fg_set & bg_set
        add_qc(qc, "foreground_in_background_count", len(overlap), "pass" if overlap else "blocked")
        add_qc(qc, "foreground_not_in_background_count", len(fg_set - bg_set), "warning" if fg_set - bg_set else "pass")
        if not bg_set:
            blocked.append("Background gene universe has no usable IDs.")
        if fg_set and not overlap:
            blocked.append("Foreground genes do not overlap the background universe.")
    else:
        status = "blocked" if args.require_background else "warning"
        add_qc(qc, "background_path", "missing", status, "ORA requires a defensible background universe.")
        if args.require_background:
            blocked.append("Background gene universe is missing.")
        else:
            warnings.append("Background gene universe is missing.")

    mapped_rows = []
    unmapped_rows = []
    if args.term2gene:
        t2g = read_term2gene(args.term2gene, args.term_column, args.gene_column)
        annotated_genes = set(t2g["gene_to_terms"])
        mapped = fg_set & annotated_genes
        unmapped = fg_set - annotated_genes
        rate = (len(mapped) / len(fg_set)) if fg_set else 0.0
        if rate < args.min_mapping_rate:
            status = "blocked"
            blocked.append(f"Mapping rate {rate:.3f} is below {args.min_mapping_rate:.3f}.")
        elif rate < args.warn_mapping_rate:
            status = "warning"
            warnings.append(f"Mapping rate {rate:.3f} is between warning thresholds.")
        else:
            status = "pass"
        add_qc(qc, "term2gene_path", args.term2gene)
        add_qc(qc, "term2gene_term_column", t2g["term_column"])
        add_qc(qc, "term2gene_gene_column", t2g["gene_column"])
        add_qc(qc, "term_count", len(t2g["term_to_genes"]))
        add_qc(qc, "annotated_gene_count", len(annotated_genes))
        add_qc(qc, "mapped_foreground_count", len(mapped), status)
        add_qc(qc, "mapping_rate", f"{rate:.6f}", status)
        for gene in sorted(mapped):
            mapped_rows.append({"gene_id": gene, "term_ids": ";".join(sorted(t2g["gene_to_terms"][gene]))})
        for gene in sorted(unmapped):
            unmapped_rows.append({"gene_id": gene, "reason": "not_in_term2gene"})
    else:
        status = "blocked" if args.require_term2gene else "warning"
        add_qc(qc, "term2gene_path", "missing", status)
        if args.require_term2gene:
            blocked.append("TERM2GENE or annotation database is missing.")

    duplicate_rows = [
        {"set": "foreground", "gene_id": gene, "count": fg["duplicate_counts"][gene]}
        for gene in fg["duplicates"]
    ]
    if args.background:
        duplicate_rows.extend(
            {"set": "background", "gene_id": gene, "count": bg["duplicate_counts"][gene]}
            for gene in bg["duplicates"]
        )

    write_tsv(outdir / "input_qc.tsv", qc, ["metric", "value", "status", "detail"])
    write_tsv(outdir / "mapped_ids.tsv", mapped_rows, ["gene_id", "term_ids"])
    write_tsv(outdir / "unmapped_ids.tsv", unmapped_rows, ["gene_id", "reason"])
    write_tsv(outdir / "duplicate_ids.tsv", duplicate_rows, ["set", "gene_id", "count"])

    if blocked:
        report = ["# Enrichment Input Check Blocked", "", "## Reasons"]
        report.extend([f"- {reason}" for reason in blocked])
        if warnings:
            report.extend(["", "## Warnings"])
            report.extend([f"- {warning}" for warning in warnings])
        report.extend(["", "See `input_qc.tsv`, `mapped_ids.tsv`, and `unmapped_ids.tsv`."])
        (outdir / "blocked_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
        print(f"blocked: {outdir / 'blocked_report.md'}")
        raise SystemExit(2)

    print(f"QC passed: {outdir / 'input_qc.tsv'}")


if __name__ == "__main__":
    main()
