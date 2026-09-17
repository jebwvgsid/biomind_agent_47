#!/usr/bin/env python3
"""Validate phenotype/covariate identities before writing in TFAM order."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from input_contract import InputError, MISSING, compare_ids, fail, numeric, numeric_token, q_row_map, split_row, table, tfam


def read_table(path, *, header_mode="auto", sample_column=None):
    parsed = table(path, header_mode=header_mode, sample_column=sample_column)
    return parsed["header"], dict(zip(parsed["ids"], parsed["rows"]))


def resolve_trait_indexes(header, traits, data):
    names = [value.strip() for value in traits.split(",") if value.strip()]
    if not names or len(set(names)) != len(names):
        fail("TRAIT_COLUMN_INVALID", "Select distinct trait/covariate columns explicitly")
    width = len(next(iter(data.values())))
    indexes = []
    for name in names:
        if header and name in header:
            index = header.index(name)
        elif name.isdigit() and 1 <= int(name) <= width:
            index = int(name) - 1
        else:
            fail("TRAIT_COLUMN_INVALID", "Use an exact header name or a valid 1-based column index; no-header names are not guessed", column=name, column_count=width)
        indexes.append(index)
    return indexes


def read_tfam_samples(path):
    return tfam(path)[0]


def clean_missing(value):
    return "NA" if value in MISSING else value


def aligned_rows(samples, data, indexes, *, allow_missing=False):
    target = [iid for _, iid in samples]
    comparison = compare_ids(target, list(data))
    if comparison["extra_ids"] or (comparison["missing_ids"] and not allow_missing):
        fail("SAMPLE_SET_MISMATCH", "Phenotype/covariate IDs differ from target TFAM IIDs; missing-row output requires explicit permission and extra rows require explicit filtering", **comparison)
    # FID is a family identifier, not an automatic substitute for an absent IID.
    rows = [[fid, iid, *([clean_missing(data[iid][i]) for i in indexes] if iid in data else ["NA"] * len(indexes))]
        for fid, iid in samples]
    for offset in range(len(indexes)):
        if all(row[offset + 2] == "NA" for row in rows):
            fail("TRAIT_ALL_MISSING", "A selected trait/covariate would contain no observed values", column=indexes[offset] + 1)
    return rows, comparison


def _write(path, rows, *, mode, names=(), covariate=False):
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        if mode == "tassel":
            if covariate:
                writer.writerow(["<Covariate>"])
            writer.writerow(["<Trait>", *names])
        for fid, iid, *values in rows:
            writer.writerow([iid, *values] if mode == "tassel" else [fid, iid, "1", *values] if mode == "covar" else [fid, iid, *values])


def write_emmax(path, samples, data, idx):
    _write(path, aligned_rows(samples, data, idx)[0], mode="emmax")


def write_emmax_covar(path, samples, data, idx):
    _write(path, aligned_rows(samples, data, idx)[0], mode="covar")


def write_tassel(path, samples, data, idx, names, covariate):
    _write(path, aligned_rows(samples, data, idx)[0], mode="tassel", names=names, covariate=covariate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tfam", required=True, type=Path)
    parser.add_argument("--phenotype", required=True, type=Path)
    parser.add_argument("--trait", required=True, help="Exact column names or 1-based indexes; no-header tables require numeric indexes")
    parser.add_argument("--header", choices=["auto", "present", "absent"], default="auto")
    parser.add_argument("--sample-column", help="Explicit header name or 1-based index, required for otherwise ambiguous numeric sample IDs")
    parser.add_argument("--input-kind", choices=["phenotype", "q_covariates"], default="phenotype")
    parser.add_argument("--q-row-map", type=Path, help="Version-locked explicit Q row assignments; never inferred from row counts")
    parser.add_argument("--allow-missing", choices=["true", "false"], default="false")
    parser.add_argument("--out-emmax", type=Path)
    parser.add_argument("--out-emmax-covar", type=Path)
    parser.add_argument("--out-tassel", type=Path)
    parser.add_argument("--tassel-covariate", nargs="?", const="true", choices=["true", "false"], default="false")
    args = parser.parse_args()
    try:
        samples, target_source = tfam(args.tfam)
        is_covariate = args.input_kind == "q_covariates" or args.out_emmax_covar is not None or args.tassel_covariate == "true"
        parsed = table(args.phenotype, header_mode=args.header, sample_column=args.sample_column, require_ids=False)
        if (is_covariate and not parsed["header_present"] and not args.q_row_map
                and all(numeric_token(value) or value in MISSING for row in parsed["rows"] for value in row)):
            fail("Q_SAMPLE_MAPPING_UNKNOWN", "A sample-column option alone cannot establish the provenance of an unlabeled numeric Q table; supply explicit version-locked row assignments")
        mapping = {"status": "ids_in_table" if parsed["ids"] else "unknown"}
        ids = parsed["ids"]
        if args.q_row_map:
            ids, mapping = q_row_map(args.q_row_map, parsed, samples, target_source)
        elif not ids:
            fail("Q_SAMPLE_MAPPING_UNKNOWN" if args.input_kind == "q_covariates" or args.out_emmax_covar else "SAMPLE_ID_COLUMN_UNRESOLVED",
                "Rows have no established sample IDs. Supply a version-locked explicit row map or clarify the sample column; equal row counts are insufficient")
        if is_covariate and args.allow_missing == "true":
            fail("Q_MISSING_ROWS_NOT_ALLOWED", "Q covariates must cover the target samples; do not silently fill missing covariates")
        data = dict(zip(ids, parsed["rows"]))
        indexes = resolve_trait_indexes(parsed["header"], args.trait, data)
        if parsed["sample_column"] in indexes:
            fail("TRAIT_IS_SAMPLE_COLUMN", "The sample ID column cannot also be selected as a trait/covariate")
        names = [parsed["header"][i] if parsed["header"] else f"V{i + 1}" for i in indexes]
        rows, comparison = aligned_rows(samples, data, indexes, allow_missing=args.allow_missing == "true")
        if is_covariate:
            invalid = [{"sample_id": row[1], "column": indexes[i] + 1} for row in rows for i, value in enumerate(row[2:]) if not numeric(value)]
            if invalid:
                fail("Q_COVARIATE_VALUE_INVALID", "Covariates must be finite numeric values; missing or categorical values require an explicit upstream scientific decision", cells=invalid[:20])
        sources = {path.resolve() for path in [args.tfam, args.phenotype, args.q_row_map] if path}
        destinations = [path.resolve() for path in [args.out_emmax, args.out_emmax_covar, args.out_tassel] if path]
        if sources.intersection(destinations) or len(destinations) != len(set(destinations)):
            fail("OUTPUT_PATH_CONFLICT", "Output paths must be distinct and cannot overwrite a formal input or mapping")
        # All input/identity/column checks complete before any output is opened.
        for path, mode in [(args.out_emmax, "emmax"), (args.out_emmax_covar, "covar"), (args.out_tassel, "tassel")]:
            if path:
                _write(path, rows, mode=mode, names=names, covariate=args.tassel_covariate == "true" or len(indexes) > 1)
        print(json.dumps({"status": "validated", "row_count": len(rows), "alignment": comparison,
            "source": parsed["source"], "target": target_source, "q_mapping": mapping}, ensure_ascii=False))
    except (InputError, OSError, UnicodeError) as exc:
        error = exc.facts if isinstance(exc, InputError) else {"code": "INPUT_READ_OR_WRITE_FAILED", "message": str(exc)}
        print(json.dumps({"status": "blocked", "error": error}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
