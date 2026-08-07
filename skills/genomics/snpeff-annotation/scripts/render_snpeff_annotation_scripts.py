#!/usr/bin/env python3
"""Render snpEff database-build and VCF-annotation shell scripts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path


class Raw(str):
    """String that should be emitted as shell syntax without quoting."""


def q(value: str | Path | Raw) -> str:
    if isinstance(value, Raw):
        return str(value)
    return shlex.quote(str(value))


def safe_genome_version(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value or ""):
        raise SystemExit(
            "--genome-version must contain only letters, digits, dot, underscore, or dash; "
            "choose a project/database key, not a free-text species name"
        )
    return value


def script_header() -> list[str]:
    return [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'SNPEFF_JAVA_HOME="${SNPEFF_JAVA_HOME:-${JAVA_HOME:-}}"',
        'if [[ -z "$SNPEFF_JAVA_HOME" && -n "${BIO_SOURCE_INSTALL_DIR:-}" && -x "${BIO_SOURCE_INSTALL_DIR}/temurin-21/bin/java" ]]; then',
        '  SNPEFF_JAVA_HOME="${BIO_SOURCE_INSTALL_DIR}/temurin-21"',
        "fi",
        'if [[ -n "$SNPEFF_JAVA_HOME" ]]; then',
        '  test -x "${SNPEFF_JAVA_HOME}/bin/java"',
        '  SNPEFF_JAR="${SNPEFF_JAR:-}"',
        '  if [[ -z "$SNPEFF_JAR" && -n "${CONDA_PREFIX:-}" ]]; then',
        '    SNPEFF_JAR="$(find "${CONDA_PREFIX}/share" -maxdepth 4 -type f -name snpEff.jar -print -quit)"',
        '  fi',
        "fi",
        'run_java() {',
        '  if [[ -n "$SNPEFF_JAVA_HOME" ]]; then',
        '    "$SNPEFF_JAVA_HOME/bin/java" "$@"',
        '  else',
        '    command java "$@"',
        '  fi',
        '}',
        'run_snpeff() {',
        '  if [[ -n "$SNPEFF_JAVA_HOME" && -n "${SNPEFF_JAR:-}" ]]; then',
        '    "$SNPEFF_JAVA_HOME/bin/java" -jar "$SNPEFF_JAR" "$@"',
        '  else',
        '    command "${SNPEFF_COMMAND:-snpEff}" "$@"',
        '  fi',
        '}',
        'require_nonempty() {',
        '  local label="$1" path="$2"',
        '  if [[ ! -s "$path" ]]; then',
        '    echo "[CHECK_FAILED] ${label} is missing or empty: ${path}" >&2',
        '    return 1',
        '  fi',
        '}',
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'cd "$SCRIPT_DIR"',
        "",
    ]


def write_script(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o700)


def snpeff_prefix(args: argparse.Namespace) -> list[str | Raw]:
    if args.snpeff_jar:
        prefix: list[str | Raw] = [Raw("run_java")]
        if args.java_options:
            prefix.extend(shlex.split(args.java_options))
        prefix.extend(["-jar", args.snpeff_jar])
        return prefix
    if args.snpeff_command == "snpEff":
        return [Raw("run_snpeff")]
    return [Raw(args.snpeff_command)]


def snpeff_cmd(args: argparse.Namespace, extra: list[str | Path | Raw]) -> str:
    parts = snpeff_prefix(args)
    extra_parts = list(extra)
    # snpEff parses the first positional token as its subcommand.  Keeping
    # build/download before global options is important: otherwise options
    # such as -gtf22 are interpreted by the default `ann` command.
    if extra_parts and str(extra_parts[0]) in {"build", "download", "databases", "dump"}:
        parts.append(extra_parts.pop(0))
    if args.config:
        parts.extend(["-c", args.config])
    elif args.use_project_config:
        parts.extend(["-c", "snpEff.project.config"])
    if args.data_dir:
        parts.extend(["-configOption", f"data.dir={args.data_dir}"])
    parts.extend(extra_parts)
    return " ".join(q(part) for part in parts)


def infer_build_format(args: argparse.Namespace) -> str:
    if args.annotation_gff3:
        return "gff3"
    if args.annotation_gtf:
        return "gtf22"
    if args.annotation_genbank:
        return "genbank"
    return ""


def has_custom_inputs(args: argparse.Namespace) -> bool:
    return bool(args.reference_fasta or args.annotation_gff3 or args.annotation_gtf or args.annotation_genbank)


def render_preflight(args: argparse.Namespace, custom: bool) -> list[str]:
    lines = script_header()
    lines.extend(
        [
            "mkdir -p logs",
            f"require_nonempty 'input VCF' {q(args.vcf)}",
            f"if ! {snpeff_cmd(args, ['-version'])} > logs/snpeff.version.txt 2>&1; then",
            "  echo '[CHECK_FAILED] snpEff runtime check failed; see logs/snpeff.version.txt' >&2",
            "  tail -n 20 logs/snpeff.version.txt >&2 || true",
            "  exit 1",
            "fi",
        ]
    )
    if args.config and not args.use_project_config:
        lines.append(f"require_nonempty 'snpEff config' {q(args.config)}")
    if custom:
        lines.extend(
            [
                f"require_nonempty 'reference FASTA' {q(args.reference_fasta)}",
                f"require_nonempty 'annotation file' {q(args.annotation_file)}",
            ]
        )
        if args.cds_fasta:
            lines.append(f"require_nonempty 'CDS FASTA' {q(args.cds_fasta)}")
        if args.protein_fasta:
            lines.append(f"require_nonempty 'protein FASTA' {q(args.protein_fasta)}")
    lines.append(f"echo '[OK] genome_version={args.genome_version} custom_build={str(custom).lower()}'")
    return lines


def render_prepare_custom_database(args: argparse.Namespace) -> list[str]:
    data_dir = args.data_dir or "data"
    genome_dir = f"{data_dir}/{args.genome_version}"
    genomes_dir = f"{data_dir}/genomes"
    genes_name = {
        "gff3": "genes.gff",
        "gtf22": "genes.gtf",
        "genbank": "genes.gbk",
    }[args.build_format]

    lines = script_header()
    lines.extend(
        [
            "mkdir -p logs",
            f"SNPEFF_DATA_DIR={q(data_dir)}",
            f"SNPEFF_GENOME={q(args.genome_version)}",
            f"SNPEFF_GENOME_DIR={q(genome_dir)}",
            f"SNPEFF_GENOMES_DIR={q(genomes_dir)}",
            'mkdir -p "$SNPEFF_GENOME_DIR" "$SNPEFF_GENOMES_DIR"',
            f"ln -sfn {q(args.reference_fasta)} \"$SNPEFF_GENOMES_DIR/$SNPEFF_GENOME.fa\"",
            f"ln -sfn {q(args.annotation_file)} \"$SNPEFF_GENOME_DIR/{genes_name}\"",
        ]
    )
    if args.cds_fasta:
        lines.append(f"ln -sfn {q(args.cds_fasta)} \"$SNPEFF_GENOME_DIR/cds.fa\"")
    if args.protein_fasta:
        lines.append(f"ln -sfn {q(args.protein_fasta)} \"$SNPEFF_GENOME_DIR/protein.fa\"")

    lines.extend(
        [
            "cat > snpEff.project.config <<EOF",
            f"data.dir = {data_dir}",
            f"{args.genome_version}.genome : {args.genome_name}",
        ]
    )
    for config_line in args.config_line:
        lines.append(config_line)
    lines.append("EOF")
    lines.append("echo '[OK] wrote snpEff.project.config and linked custom database inputs'")
    return lines


def render_build_database(args: argparse.Namespace) -> list[str]:
    lines = script_header()
    build_flag = {
        "gff3": "-gff3",
        "gtf22": "-gtf22",
        "genbank": "-genbank",
    }[args.build_format]
    extra = ["build", build_flag, "-v"]
    if not args.cds_fasta:
        extra.append("-noCheckCds")
    if not args.protein_fasta:
        extra.append("-noCheckProtein")
    extra.append(args.genome_version)
    lines.extend(
        [
            "mkdir -p logs",
            "set +e",
            f"{snpeff_cmd(args, extra)} > logs/snpeff.build.log 2>&1",
            "build_rc=$?",
            "set -e",
            "if (( build_rc != 0 )); then",
            "  echo \"[STEP_FAILED] snpEff database build exited with ${build_rc}\" >&2",
            "  if [[ -s logs/snpeff.build.log ]]; then",
            "    echo '===== tail: logs/snpeff.build.log =====' >&2",
            "    tail -n 100 logs/snpeff.build.log >&2",
            "  else",
            "    echo '[CHECK_FAILED] snpEff build log is missing or empty' >&2",
            "  fi",
            "  exit \"$build_rc\"",
            "fi",
            f"test -s {q(Path(args.data_dir) / args.genome_version / 'snpEffectPredictor.bin')}",
            "if grep -Eq '(^|[[:space:]])FATAL([:[:space:]]|$)|ERROR: Database check failed' logs/snpeff.build.log; then",
            "  echo '[CHECK_FAILED] snpEff build log contains a terminal failure' >&2",
            "  tail -n 100 logs/snpeff.build.log >&2",
            "  exit 1",
            "fi",
        ]
    )
    return lines


def render_download_database(args: argparse.Namespace) -> list[str]:
    lines = script_header()
    lines.extend(
        [
            "mkdir -p logs",
            f"{snpeff_cmd(args, ['databases'])} > logs/snpeff.databases.txt 2>&1 || true",
            f"{snpeff_cmd(args, ['download', '-v', args.genome_version])} > logs/snpeff.download.log 2>&1",
        ]
    )
    return lines


def render_annotation(args: argparse.Namespace) -> list[str]:
    out_vcf = args.output_vcf or f"{Path(args.vcf).name}.snpeff.ann.vcf"
    lines = script_header()
    ann_args: list[str | Path | Raw] = [
        "-v",
        "-stats",
        "snpEff_summary.html",
        args.genome_version,
        args.vcf,
    ]
    lines.extend(
        [
            "mkdir -p logs",
            *(
                [f"test -s {q(Path(args.data_dir) / args.genome_version / 'snpEffectPredictor.bin')}"]
                if has_custom_inputs(args) else []
            ),
            f"{snpeff_cmd(args, ann_args)} > {q(out_vcf)} 2> logs/snpeff.annotate.log",
            f"grep -q '^##INFO=<ID=ANN' {q(out_vcf)}",
            f"grep -q '^##SnpEffVersion' {q(out_vcf)} || true",
            f"echo '[OK] annotated VCF: {out_vcf}'",
        ]
    )
    if args.compress_output:
        lines.extend(
            [
                f"if command -v bgzip >/dev/null 2>&1; then bgzip -f {q(out_vcf)}; fi",
                f"if command -v tabix >/dev/null 2>&1 && [[ -s {q(out_vcf + '.gz')} ]]; then tabix -f -p vcf {q(out_vcf + '.gz')}; fi",
            ]
        )
    return lines


def render_ann_summary(args: argparse.Namespace) -> list[str]:
    out_vcf = args.output_vcf or f"{Path(args.vcf).name}.snpeff.ann.vcf"
    lines = script_header()
    lines.extend([
        f"require_nonempty 'annotated VCF' {q(out_vcf)}",
        "awk -F '\\t' '",
        "BEGIN { OFS=\"\\t\"; print \"impact\", \"effect\", \"count\" }",
        "/^#/ { next }",
        "{",
        "  n_info=split($8, info, \";\"); ann=\"\"",
        "  for (i=1; i<=n_info; i++) if (info[i] ~ /^ANN=/) { ann=substr(info[i], 5); break }",
        "  if (ann == \"\") next",
        "  n_ann=split(ann, records, \",\")",
        "  for (i=1; i<=n_ann; i++) {",
        "    split(records[i], fields, \"|\")",
        "    effect=(fields[2] == \"\" ? \"UNKNOWN\" : fields[2])",
        "    impact=(fields[3] == \"\" ? \"UNKNOWN\" : fields[3])",
        "    counts[impact SUBSEP effect]++",
        "  }",
        "}",
        "END { for (key in counts) { split(key, parts, SUBSEP); print parts[1], parts[2], counts[key] } }",
        f"' {q(out_vcf)} > ann_effect_summary.tsv",
        "require_nonempty 'ANN effect summary' ann_effect_summary.tsv",
        "awk -F '\\t' '",
        "BEGIN { OFS=\"\\t\"; print \"gene_name\", \"gene_id\", \"impact\", \"effect\", \"count\" }",
        "/^#/ { next }",
        "{",
        "  n_info=split($8, info, \";\"); ann=\"\"",
        "  for (i=1; i<=n_info; i++) if (info[i] ~ /^ANN=/) { ann=substr(info[i], 5); break }",
        "  if (ann == \"\") next",
        "  n_ann=split(ann, records, \",\")",
        "  for (i=1; i<=n_ann; i++) {",
        "    split(records[i], fields, \"|\")",
        "    effect=(fields[2] == \"\" ? \"UNKNOWN\" : fields[2])",
        "    impact=(fields[3] == \"\" ? \"UNKNOWN\" : fields[3])",
        "    gene_name=(fields[4] == \"\" ? \"UNKNOWN\" : fields[4])",
        "    gene_id=(fields[5] == \"\" ? \"UNKNOWN\" : fields[5])",
        "    counts[gene_name SUBSEP gene_id SUBSEP impact SUBSEP effect]++",
        "  }",
        "}",
        "END { for (key in counts) { split(key, parts, SUBSEP); print parts[1], parts[2], parts[3], parts[4], counts[key] } }",
        f"' {q(out_vcf)} > ann_gene_summary.tsv",
        "require_nonempty 'ANN gene summary' ann_gene_summary.tsv",
        "echo '[OK] wrote ann_effect_summary.tsv and ann_gene_summary.tsv'",
    ])
    return lines


def render_finalize(args: argparse.Namespace) -> list[str]:
    out_vcf = args.output_vcf or f"{Path(args.vcf).name}.snpeff.ann.vcf"
    validation = validation_metadata(args)
    mode = str(validation.get("validation_mode") or "strict")
    skipped = ",".join(validation.get("skipped_checks") or []) or "none"
    limitation = str(validation.get("scientific_limitation") or "None")
    custom = has_custom_inputs(args)
    pipeline_scripts = (
        "00.preflight.sh 01.prepare_custom_database.sh 02.build_database.sh "
        "03.annotate_vcf.sh 04.summarize_ann.sh 05.finalize.sh"
        if custom else
        "00.preflight.sh 02.download_database.sh 03.annotate_vcf.sh 04.summarize_ann.sh 05.finalize.sh"
    )
    lines = script_header()
    if custom:
        lines.append(
            f"require_nonempty 'database predictor' {q(Path(args.data_dir) / args.genome_version / 'snpEffectPredictor.bin')}"
        )
    lines.extend([
        f"require_nonempty 'annotated VCF' {q(out_vcf)}",
        "require_nonempty 'snpEff HTML summary' snpEff_summary.html",
        "require_nonempty 'ANN effect summary' ann_effect_summary.tsv",
        "require_nonempty 'ANN gene summary' ann_gene_summary.tsv",
        "cat > stage_status.tsv <<'STATUS_EOF'",
        "stage\tstatus\toutput_path\tnotes",
        "runtime_preflight\tcompleted\tlogs/snpeff.version.txt\tverified runtime",
        (
            f"database_build\tcompleted\tdata/{args.genome_version}/snpEffectPredictor.bin\tvalidation_mode={mode}; skipped_checks={skipped}"
            if custom else
            f"database_selection\tcompleted\t{args.genome_version}\texisting database"
        ),
        f"annotation\tcompleted\t{out_vcf}\tANN header verified",
        "summary\tcompleted\tann_effect_summary.tsv;ann_gene_summary.tsv\teffect, impact, and gene counts",
        "STATUS_EOF",
        "cat > report.md <<'REPORT_EOF'",
        "# snpEff Custom Genome Annotation",
        "",
        f"- Genome database key: `{args.genome_version}`",
        f"- Input VCF: `{Path(args.vcf).resolve()}`",
        f"- Reference FASTA: `{Path(args.reference_fasta).resolve() if args.reference_fasta else ''}`",
        f"- Annotation: `{Path(args.annotation_file).resolve() if args.annotation_file else ''}`",
        f"- Database validation mode: `{mode}`",
        f"- Skipped checks: `{skipped}`",
        f"- Scientific limitation: {limitation}",
        "",
        "The database was built with the format-specific snpEff CLI option and the annotated VCF was verified to contain the ANN header.",
        "REPORT_EOF",
        "cat > reproduce.sh <<'REPRODUCE_EOF'",
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'cd "$SCRIPT_DIR"',
        f"# Renderer validation mode: {mode}",
        f"for script in {pipeline_scripts}; do",
        '  bash "$SCRIPT_DIR/$script"',
        "done",
        "REPRODUCE_EOF",
        "chmod +x reproduce.sh",
        "require_nonempty 'stage status' stage_status.tsv",
        "require_nonempty 'report' report.md",
        "require_nonempty 'reproduction script' reproduce.sh",
        "echo '[OK] wrote final report, status, and reproduction artifacts'",
    ])
    return lines


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    args.genome_version = safe_genome_version(args.genome_version)
    custom = has_custom_inputs(args)
    ann_files = [args.annotation_gff3, args.annotation_gtf, args.annotation_genbank]
    ann_count = sum(bool(item) for item in ann_files)
    if custom:
        if ann_count != 1:
            raise SystemExit("Custom builds require exactly one of --annotation-gff3, --annotation-gtf, or --annotation-genbank")
        if not args.reference_fasta:
            raise SystemExit("Custom builds require --reference-fasta")
        args.build_format = infer_build_format(args)
        args.annotation_file = args.annotation_gff3 or args.annotation_gtf or args.annotation_genbank
        args.use_project_config = not args.config
        if args.data_dir is None:
            args.data_dir = "data"
    else:
        if args.allow_unchecked_build or args.cds_fasta or args.protein_fasta or args.config_line:
            raise SystemExit("Build-only options require custom database inputs")
        args.build_format = ""
        args.annotation_file = ""
        args.use_project_config = False
    if args.snpeff_jar and args.snpeff_command != "snpEff":
        raise SystemExit("Use either --snpeff-jar or --snpeff-command, not both")
    if args.allow_unchecked_build and args.cds_fasta and args.protein_fasta:
        raise SystemExit("Do not use --allow-unchecked-build when both CDS and protein validation FASTA files are provided")
    args.genome_name = args.genome_name or args.genome_version
    return args


def validation_metadata(args: argparse.Namespace) -> dict:
    missing = []
    if has_custom_inputs(args):
        if not args.cds_fasta:
            missing.append("CDS")
        if not args.protein_fasta:
            missing.append("protein")
    if missing:
        return {
            "validation_mode": "optional_validation_skipped",
            "skipped_checks": missing,
            "skipped_status": "skipped_optional_missing",
            "reason": "cds.fa and/or protein.fa were not provided",
            "scientific_limitation": (
                "The custom snpEff database was built without all CDS/protein consistency validation."
            ),
            "required_disclosures": ["report.md", "stage_status.tsv", "reproduce.sh"],
            "reproduction_markers": [],
        }
    return {
        "validation_mode": "strict",
        "skipped_checks": [],
        "missing_validation_assets": missing,
        "reason": "Full snpEff database checks are required.",
        "scientific_limitation": "",
        "required_disclosures": [],
        "reproduction_markers": [],
    }


def requirement_manifest(args: argparse.Namespace) -> list[dict]:
    requirements = [
        {"requirement_id": "input_vcf", "role": "variant_input", "path": args.vcf, "level": "required", "present": True},
        {
            "requirement_id": "snpeff_runtime",
            "role": "runtime",
            "value": args.snpeff_jar or args.snpeff_command,
            "level": "required",
            "present": bool(args.snpeff_jar or args.snpeff_command),
        },
    ]
    if has_custom_inputs(args):
        requirements.extend([
            {"requirement_id": "reference_fasta", "role": "reference", "path": args.reference_fasta, "level": "required", "present": bool(args.reference_fasta)},
            {"requirement_id": "annotation_source", "role": "annotation", "path": args.annotation_file, "level": "required", "present": bool(args.annotation_file)},
            {"requirement_id": "cds_validation", "role": "validation_input", "path": args.cds_fasta or "", "level": "optional_validation", "present": bool(args.cds_fasta)},
            {"requirement_id": "protein_validation", "role": "validation_input", "path": args.protein_fasta or "", "level": "optional_validation", "present": bool(args.protein_fasta)},
        ])
    return requirements


def action_manifest(scripts: dict[str, list[str]], artifacts: list[dict]) -> list[dict]:
    artifact_ids: dict[str, list[str]] = {}
    for artifact in artifacts:
        producer = str(artifact.get("producer") or "")
        if producer:
            artifact_ids.setdefault(producer, []).append(str(artifact.get("artifact_id") or ""))
    kinds = {"00.preflight.sh": "verification"}
    actions = []
    previous = ""
    for index, name in enumerate(scripts, start=1):
        action_id = re.sub(r"[^a-z0-9]+", "_", Path(name).stem.lower()).strip("_") or f"action_{index}"
        actions.append({
            "id": action_id,
            "script": name,
            "kind": kinds.get(name, "primary"),
            "depends_on": [previous] if previous else [],
            "expected_artifact_ids": artifact_ids.get(name, []),
            "success_checks": ["script_exit_zero", *(["expected_artifacts_nonempty"] if artifact_ids.get(name) else [])],
            "required": True,
            "enabled": True,
        })
        previous = action_id
    return actions


def _path_fingerprint(path: str) -> dict:
    candidate = Path(path).resolve() if path else None
    if not candidate:
        return {"path": "", "size": 0, "mtime_ns": 0}
    try:
        stat = candidate.stat()
        return {"path": str(candidate), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError:
        return {"path": str(candidate), "size": 0, "mtime_ns": 0}


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def artifact_manifest(args: argparse.Namespace, scripts: dict[str, list[str]], outdir: Path) -> list[dict]:
    producer = Path(__file__).name
    step_id = str(args.producer_step_id or "")
    generated_at = datetime.now(timezone.utc).isoformat()
    producer_action_id = str(os.environ.get("BIOMIND_ACTION_ID") or "")
    script_ids = {
        "00.preflight.sh": "snpeff_runtime_preflight_script",
        "01.prepare_custom_database.sh": "snpeff_database_prepare_script",
        "02.build_database.sh": "snpeff_database_build_script",
        "02.download_database.sh": "snpeff_database_download_script",
        "03.annotate_vcf.sh": "snpeff_annotation_script",
        "04.summarize_ann.sh": "snpeff_ann_summary_script",
        "05.finalize.sh": "snpeff_finalize_script",
    }
    artifacts = [
        {
            "artifact_id": script_ids.get(name, f"snpeff_{Path(name).stem}_script"),
            "path": name,
            "artifact_type": "script",
            "required": True,
            "producer_step_id": step_id,
            "producer_action_id": producer_action_id,
            "producer": producer,
            "validator": "nonempty_executable_script",
            "state": "generated",
            "generated_at": generated_at,
        }
        for name in scripts
    ]
    if has_custom_inputs(args):
        artifacts.append({
            "artifact_id": "snpeff_database_predictor",
            "path": str(Path(args.data_dir) / args.genome_version / "snpEffectPredictor.bin"),
            "artifact_type": "database",
            "required": True,
            "producer_step_id": "",
            "producer_action_id": "",
            "producer": "02.build_database.sh",
            "validator": "nonempty_file",
            "state": "promised",
        })
    artifacts.extend([
        {
            "artifact_id": "snpeff_annotated_vcf",
            "path": str(args.output_vcf or f"{Path(args.vcf).name}.snpeff.ann.vcf"),
            "artifact_type": "vcf",
            "required": True,
            "producer_step_id": "",
            "producer_action_id": "",
            "producer": "03.annotate_vcf.sh",
            "validator": "nonempty_vcf_with_ann_header",
            "state": "promised",
        },
        {
            "artifact_id": "snpeff_summary_html",
            "path": "snpEff_summary.html",
            "artifact_type": "report",
            "required": True,
            "producer_step_id": "",
            "producer_action_id": "",
            "producer": "03.annotate_vcf.sh",
            "validator": "nonempty_file",
            "state": "promised",
        },
        {
            "artifact_id": "snpeff_ann_effect_summary",
            "path": "ann_effect_summary.tsv",
            "artifact_type": "table",
            "required": True,
            "producer_step_id": "",
            "producer_action_id": "",
            "producer": "04.summarize_ann.sh",
            "validator": "nonempty_file",
            "state": "promised",
        },
        {
            "artifact_id": "snpeff_ann_gene_summary",
            "path": "ann_gene_summary.tsv",
            "artifact_type": "table",
            "required": True,
            "producer_step_id": "",
            "producer_action_id": "",
            "producer": "04.summarize_ann.sh",
            "validator": "nonempty_file",
            "state": "promised",
        },
        *[
            {
                "artifact_id": f"snpeff_{Path(path).stem}",
                "path": path,
                "artifact_type": "report" if path.endswith(".md") else "reproduction" if path.endswith(".sh") else "status",
                "required": True,
                "producer_step_id": "",
                "producer_action_id": "",
                "producer": "05.finalize.sh",
                "validator": "nonempty_executable_script" if path.endswith(".sh") else "nonempty_file",
                "state": "promised",
            }
            for path in ("report.md", "reproduce.sh", "stage_status.tsv")
        ],
    ])
    return artifacts


def output_manifest(artifacts: list[dict], producer_step_id: str = "") -> list[dict]:
    """Declare only canonical final outputs for the controller output gate."""
    final_ids = {
        "snpeff_annotated_vcf",
        "snpeff_summary_html",
        "snpeff_ann_effect_summary",
        "snpeff_ann_gene_summary",
        "snpeff_report",
        "snpeff_reproduce",
        "snpeff_stage_status",
    }
    return [
        {
            "output_id": str(item.get("artifact_id") or ""),
            "path": str(item.get("path") or ""),
            "required": bool(item.get("required", True)),
            "producer_step_id": str(producer_step_id or ""),
            "renderer_id": "genomics.snpeff_annotation",
        }
        for item in artifacts
        if str(item.get("artifact_id") or "") in final_ids
    ]


def validate_render_inputs(args: argparse.Namespace) -> None:
    """Reject explicit input/runtime paths before emitting unusable scripts."""
    paths = [("input VCF", args.vcf)]
    if args.snpeff_jar:
        paths.append(("snpEff JAR", args.snpeff_jar))
    if args.config and not args.use_project_config:
        paths.append(("snpEff config", args.config))
    if has_custom_inputs(args):
        paths.extend([
            ("reference FASTA", args.reference_fasta),
            ("annotation file", args.annotation_file),
        ])
    if args.cds_fasta:
        paths.append(("CDS FASTA", args.cds_fasta))
    if args.protein_fasta:
        paths.append(("protein FASTA", args.protein_fasta))

    invalid = [f"{label}: {path}" for label, path in paths if not path or not Path(path).is_file() or Path(path).stat().st_size == 0]
    if invalid:
        raise SystemExit("Missing or empty explicit renderer input(s):\n- " + "\n- ".join(invalid))


def render_script_bundle(args: argparse.Namespace, custom: bool) -> dict[str, list[str]]:
    scripts: dict[str, list[str]] = {"00.preflight.sh": render_preflight(args, custom)}
    if custom:
        scripts["01.prepare_custom_database.sh"] = render_prepare_custom_database(args)
        scripts["02.build_database.sh"] = render_build_database(args)
    else:
        scripts["02.download_database.sh"] = render_download_database(args)
    scripts["03.annotate_vcf.sh"] = render_annotation(args)
    scripts["04.summarize_ann.sh"] = render_ann_summary(args)
    scripts["05.finalize.sh"] = render_finalize(args)
    return scripts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render snpEff annotation scripts")
    parser.add_argument("--vcf", required=True, help="Input VCF/VCF.GZ to annotate")
    parser.add_argument("--genome-version", required=True, help="snpEff database key; must match config/data directory")
    parser.add_argument("--outdir", default="snpeff_annotation", help="Directory for rendered scripts")
    parser.add_argument("--output-vcf", help="Annotated VCF output path inside the rendered script directory")
    parser.add_argument(
        "--snpeff-jar",
        default=os.environ.get("SNPEFF_JAR") or "",
        help="Path to snpEff.jar (defaults to the verified SNPEFF_JAR runtime binding)",
    )
    parser.add_argument("--snpeff-command", default="snpEff", help="snpEff command on PATH when --snpeff-jar is not used")
    parser.add_argument("--java-options", default="-Xmx8g", help="Java options used with --snpeff-jar")
    parser.add_argument("--config", help="Existing snpEff.config path")
    parser.add_argument("--data-dir", help="snpEff data directory")
    parser.add_argument("--reference-fasta", help="Reference FASTA for a custom database")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--annotation-gff3", help="GFF3 annotation for a custom database")
    group.add_argument("--annotation-gtf", help="GTF annotation for a custom database")
    group.add_argument("--annotation-genbank", help="GenBank annotation for a custom database")
    parser.add_argument("--genome-name", help="Human-readable genome name for generated project config")
    parser.add_argument("--cds-fasta", help="Optional CDS FASTA for database checks")
    parser.add_argument("--protein-fasta", help="Optional protein FASTA for database checks")
    parser.add_argument("--config-line", action="append", default=[], help="Extra line appended to generated project config")
    parser.add_argument(
        "--allow-unchecked-build",
        action="store_true",
        help="Deprecated compatibility flag; missing optional CDS/protein checks are represented by requirement levels",
    )
    parser.add_argument("--producer-step-id", default="", help="Optional plan step id recorded in the artifact manifest")
    parser.add_argument("--compress-output", action="store_true", help="Use bgzip/tabix after annotation when available")
    return normalize_args(parser.parse_args())


def main() -> int:
    args = parse_args()
    validate_render_inputs(args)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    custom = has_custom_inputs(args)

    scripts = render_script_bundle(args, custom)

    for name, lines in scripts.items():
        write_script(outdir / name, lines)

    if custom:
        build_script = (outdir / "02.build_database.sh").read_text(encoding="utf-8")
        if not re.search(r"(^|\s)-gtf22(?:\s|$)", build_script, re.MULTILINE) and args.build_format == "gtf22":
            raise SystemExit("Rendered GTF database build script does not contain the required -gtf22 CLI flag")

    generated_at = datetime.now(timezone.utc).isoformat()
    artifacts = artifact_manifest(args, scripts, outdir)
    requirements = requirement_manifest(args)
    actions = action_manifest(scripts, artifacts)
    input_fingerprint = _fingerprint([
        _path_fingerprint(path)
        for path in (args.vcf, args.reference_fasta, args.annotation_file, args.cds_fasta, args.protein_fasta)
        if path
    ])
    runtime_fingerprint = _fingerprint({
        "snpeff_jar": _path_fingerprint(args.snpeff_jar) if args.snpeff_jar else {},
        "snpeff_command": args.snpeff_command,
        "java_home": os.environ.get("SNPEFF_JAVA_HOME") or os.environ.get("JAVA_HOME") or "",
    })
    options_fingerprint = _fingerprint({
        "genome_version": args.genome_version,
        "build_format": args.build_format,
        "data_dir": args.data_dir,
        "config_line": args.config_line,
        "java_options": args.java_options,
    })
    manifest = {
        "schema_version": 3,
        "manifest_type": "renderer_artifact_contract",
        "producer": Path(__file__).name,
        "producer_step_id": str(args.producer_step_id or ""),
        "producer_action_id": str(os.environ.get("BIOMIND_ACTION_ID") or ""),
        "task_id": str(os.environ.get("BIOMIND_TASK_ID") or ""),
        "generated_at": generated_at,
        "renderer": {"id": "genomics.snpeff_annotation", "version": 3},
        "provenance": {
            "task_id": str(os.environ.get("BIOMIND_TASK_ID") or ""),
            "step_id": str(args.producer_step_id or ""),
            "producer_action_id": str(os.environ.get("BIOMIND_ACTION_ID") or ""),
            "input_fingerprint": input_fingerprint,
            "runtime_fingerprint": runtime_fingerprint,
            "options_fingerprint": options_fingerprint,
        },
        "genome_version": args.genome_version,
        "custom_build": custom,
        "scripts": list(scripts),
        "outdir": str(outdir),
        "inputs": {
            "vcf": str(Path(args.vcf).resolve()),
            "reference_fasta": str(Path(args.reference_fasta).resolve()) if args.reference_fasta else "",
            "annotation": str(Path(args.annotation_file).resolve()) if args.annotation_file else "",
        },
        "runtime": {
            "snpeff_jar": str(Path(args.snpeff_jar).resolve()) if args.snpeff_jar else "",
            "java_home": str(os.environ.get("SNPEFF_JAVA_HOME") or os.environ.get("JAVA_HOME") or ""),
        },
        "actions": actions,
        "outputs": output_manifest(artifacts, str(args.producer_step_id or "")),
        "requirements": requirements,
        "artifacts": artifacts,
        "validation": validation_metadata(args),
        "tool_metadata": {
            "tool": "snpEff",
            "genome_version": args.genome_version,
            "custom_build": custom,
            "build_format": args.build_format,
        },
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
