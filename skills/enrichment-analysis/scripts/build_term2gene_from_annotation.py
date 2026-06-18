#!/usr/bin/env python3
"""Build a TERM2GENE table from a tabular gene annotation file."""

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path


MISSING = {"", "na", "nan", "none", "null", "-", ".", "no_hit", "unknown"}
AUTO_TERM_PATTERNS = (
    "go",
    "kegg",
    "ko",
    "pathway",
    "pfam",
    "interpro",
    "ipr",
    "eggnog",
    "cog",
    "kog",
    "mapman",
    "term",
)


def sniff_delimiter(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    sample = "\n".join([line for line in text.splitlines() if line.strip() and not line.startswith("#")][:5])
    if "\t" in sample:
        return "\t"
    if "," in sample:
        return ","
    return None


def read_table(path):
    delim = sniff_delimiter(path)
    with Path(path).open("r", encoding="utf-8", errors="replace", newline="") as handle:
        lines = [line for line in handle if line.strip() and not line.startswith("#")]
    if delim is None:
        header = re.split(r"\s+", lines[0].strip())
        rows = []
        for line in lines[1:]:
            parts = re.split(r"\s+", line.rstrip("\n"), maxsplit=len(header) - 1)
            rows.append({header[i]: parts[i] if i < len(parts) else "" for i in range(len(header))})
        return header, rows
    reader = csv.DictReader(lines, delimiter=delim)
    return list(reader.fieldnames or []), list(reader)


def choose_gene_column(columns, requested):
    if requested:
        for col in columns:
            if col == requested or col.lower() == requested.lower():
                return col
        raise SystemExit(f"gene column not found: {requested}")
    for candidate in ("gene_id", "gene", "id", "GeneID", "locus", "protein_id", "query"):
        for col in columns:
            if col == candidate or col.lower() == candidate.lower():
                return col
    return columns[0]


def choose_term_columns(columns, requested):
    if requested:
        wanted = [item.strip() for item in requested.split(",") if item.strip()]
        chosen = []
        lower = {col.lower(): col for col in columns}
        for item in wanted:
            if item in columns:
                chosen.append(item)
            elif item.lower() in lower:
                chosen.append(lower[item.lower()])
            else:
                raise SystemExit(f"term column not found: {item}")
        return chosen
    chosen = []
    for col in columns:
        low = col.lower()
        if any(pattern in low for pattern in AUTO_TERM_PATTERNS):
            chosen.append(col)
    if not chosen:
        raise SystemExit("No term columns found. Provide --term-columns.")
    return chosen


def clean_terms(value, split_regex):
    value = str(value).strip()
    if value.lower() in MISSING:
        return []
    pieces = [piece.strip() for piece in re.split(split_regex, value) if piece.strip()]
    terms = []
    for piece in pieces:
        if piece.lower() in MISSING:
            continue
        for go in re.findall(r"GO:\d{7}", piece):
            terms.append(go)
        ko_matches = re.findall(r"(?:ko:)?K\d{5}", piece)
        for ko in ko_matches:
            terms.append(ko.replace("ko:", ""))
        ipr_matches = re.findall(r"IPR\d{6}", piece)
        terms.extend(ipr_matches)
        pf_matches = re.findall(r"PF\d{5}", piece)
        terms.extend(pf_matches)
        if not (re.search(r"GO:\d{7}", piece) or re.search(r"(?:ko:)?K\d{5}", piece) or re.search(r"IPR\d{6}", piece) or re.search(r"PF\d{5}", piece)):
            terms.append(piece)
    return sorted(set(terms))


def write_tsv(path, rows, header):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in header})


def main():
    parser = argparse.ArgumentParser(description="Build TERM2GENE from a gene annotation table.")
    parser.add_argument("--annotation", required=True)
    parser.add_argument("--gene-column")
    parser.add_argument("--term-columns", help="Comma-separated annotation columns to parse.")
    parser.add_argument("--split-regex", default=r"[;,|]+")
    parser.add_argument("--out-term2gene", required=True)
    parser.add_argument("--out-term2name")
    parser.add_argument("--summary")
    parser.add_argument("--min-term-size", type=int, default=1)
    parser.add_argument("--max-term-size", type=int, default=1000000000)
    args = parser.parse_args()

    columns, rows = read_table(args.annotation)
    if not columns:
        raise SystemExit("annotation table has no columns")
    gene_col = choose_gene_column(columns, args.gene_column)
    term_cols = choose_term_columns(columns, args.term_columns)

    term_to_genes = defaultdict(set)
    term_sources = defaultdict(set)
    genes_seen = set()
    for row in rows:
        gene = str(row.get(gene_col, "")).strip()
        if gene.lower() in MISSING:
            continue
        genes_seen.add(gene)
        for col in term_cols:
            for term in clean_terms(row.get(col, ""), args.split_regex):
                term_to_genes[term].add(gene)
                term_sources[term].add(col)

    term2gene_rows = []
    term2name_rows = []
    for term in sorted(term_to_genes):
        genes = sorted(term_to_genes[term])
        if len(genes) < args.min_term_size or len(genes) > args.max_term_size:
            continue
        for gene in genes:
            term2gene_rows.append({"term_id": term, "gene_id": gene})
        term2name_rows.append({"term_id": term, "term_name": term, "source_column": ";".join(sorted(term_sources[term]))})

    write_tsv(args.out_term2gene, term2gene_rows, ["term_id", "gene_id"])
    if args.out_term2name:
        write_tsv(args.out_term2name, term2name_rows, ["term_id", "term_name", "source_column"])
    if args.summary:
        summary = [
            {"metric": "annotation", "value": args.annotation},
            {"metric": "gene_column", "value": gene_col},
            {"metric": "term_columns", "value": ",".join(term_cols)},
            {"metric": "genes_seen", "value": len(genes_seen)},
            {"metric": "terms_written", "value": len(term2name_rows)},
            {"metric": "term2gene_rows", "value": len(term2gene_rows)},
            {"metric": "min_term_size", "value": args.min_term_size},
            {"metric": "max_term_size", "value": args.max_term_size},
        ]
        write_tsv(args.summary, summary, ["metric", "value"])
    print(f"wrote {len(term2gene_rows)} TERM2GENE rows -> {args.out_term2gene}")


if __name__ == "__main__":
    main()
