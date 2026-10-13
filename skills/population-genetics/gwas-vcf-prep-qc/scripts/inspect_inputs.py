#!/usr/bin/env python3
"""Observe bounded GWAS input structure without scientific tools or outputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from input_contract import InputError, compare_ids, fail, q_row_map, table, tfam, vcf_header


def inspect_inputs(vcf, phenotype, q_matrix=None, *, gff3=None, header_mode="auto", sample_column=None, row_map=None, tfam_path=None):
    vcf_info = vcf_header(vcf)
    phenotype_info = table(phenotype, header_mode=header_mode, sample_column=sample_column)
    comparison = compare_ids(vcf_info["sample_ids"], phenotype_info["ids"])
    observation = {"schema_version": 1, "producer": "gwas_input_structure_v1", "status": "observed",
        # File bytes/read scope are business observations. The generic context
        # projector reserves "source" for control provenance and removes it.
        # Keep the parser's internal source contract, but publish file_identity
        # so accepted observations retain their exact file version to consumers.
        "vcf": {**{key: value for key, value in vcf_info.items() if key != "source"}, "file_identity": vcf_info["source"]},
        "phenotype": {**{key: phenotype_info[key] for key in ("header", "header_present", "sample_column", "row_count", "column_count", "ids")},
            "file_identity": phenotype_info["source"]},
        "vcf_phenotype_alignment": comparison, "q_mapping": {"status": "not_supplied"}, "blocked_operations": [],
        "scientific_completion": False}
    if not comparison["same_ids"]:
        observation["blocked_operations"].extend(["phenotype_preparation", "association"])
    if q_matrix:
        q = table(q_matrix, require_ids=False)
        observation["q_matrix"] = {**{key: q[key] for key in ("header", "header_present", "sample_column", "row_count", "column_count")},
            "file_identity": q["source"]}
        mapping = {"status": "unknown", "reason_code": "Q_SAMPLE_MAPPING_UNKNOWN",
            "reason": "Q rows have no IDs; equal row counts do not establish sample correspondence or order"}
        if row_map:
            if not tfam_path:
                fail("Q_ROW_MAP_TARGET_REQUIRED", "The explicit map must be checked against its exact target TFAM file")
            pairs, target = tfam(tfam_path)
            if [iid for _, iid in pairs] != vcf_info["sample_ids"]:
                fail("Q_ROW_MAP_TARGET_ORDER_MISMATCH", "The map's target TFAM does not match the inspected VCF sample order")
            _, mapping = q_row_map(row_map, q, pairs, target)
        elif q["ids"]:
            aligned = compare_ids(vcf_info["sample_ids"], q["ids"])
            mapping = {"status": "ids_in_table" if aligned["same_ids"] else "blocked", "comparison": aligned}
        if mapping["status"] in {"unknown", "blocked"}:
            observation["blocked_operations"].extend(["q_covariate_preparation", "association_using_q"])
        observation["q_mapping"] = mapping
    if gff3:
        # A prefix observation is not full annotation validation or versioning.
        with gff3.open("rb") as handle:
            prefix = handle.read(65536)
        lines = prefix.decode("utf-8").splitlines()
        first = next((line for line in lines if line and not line.startswith("#")), "")
        observation["gff3"] = {"path": str(gff3.resolve()), "bytes_read": len(prefix), "identity_scope": "prefix_only",
            "prefix_sha256": hashlib.sha256(prefix).hexdigest(), "file_bytes": gff3.stat().st_size,
            "first_feature_columns": len(first.split("\t")) if first else None, "whole_file_validated": False}
    observation["blocked_operations"] = list(dict.fromkeys(observation["blocked_operations"]))
    # Surface the scientific stop before potentially long sample-ID lists. The
    # legacy status means the inspection ran; it is not permission to associate.
    return {"decision": "blocked_need_user_evidence" if observation["blocked_operations"] else "input_identity_validated",
        "next_action": ("STOP the dependent association stage. In noninteractive benchmark write blocked/report/reproduce and end; "
                        "in interactive mode ask for the missing sample mapping/provenance. Do not infer Q order, "
                        "drop Q, change model, or continue software discovery for this blocked stage."
                        if observation["blocked_operations"] else
                        "Input identity checks passed; continue the requested downstream checks using this explicit mapping."),
        "blocked_operations": observation["blocked_operations"], "q_mapping": observation["q_mapping"], **observation}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vcf", required=True, type=Path)
    parser.add_argument("--phenotype", required=True, type=Path)
    parser.add_argument("--q-matrix", type=Path)
    parser.add_argument("--gff3", type=Path)
    parser.add_argument("--phenotype-header", choices=["auto", "present", "absent"], default="auto")
    parser.add_argument("--sample-column")
    parser.add_argument("--q-row-map", type=Path)
    parser.add_argument("--tfam", type=Path)
    parser.add_argument("--observation-id", help="Optional result label")
    args = parser.parse_args()
    try:
        if args.q_row_map and not args.q_matrix:
            fail("Q_ROW_MAP_INPUT_REQUIRED", "A row map requires the corresponding Q matrix")
        value = inspect_inputs(args.vcf, args.phenotype, args.q_matrix, gff3=args.gff3,
            header_mode=args.phenotype_header, sample_column=args.sample_column, row_map=args.q_row_map, tfam_path=args.tfam)
        print(json.dumps(value, ensure_ascii=False))
    except (InputError, OSError, UnicodeError) as exc:
        error = exc.facts if isinstance(exc, InputError) else {"code": "INPUT_OBSERVATION_FAILED", "message": str(exc)}
        print(json.dumps({"status": "blocked", "error": error}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
