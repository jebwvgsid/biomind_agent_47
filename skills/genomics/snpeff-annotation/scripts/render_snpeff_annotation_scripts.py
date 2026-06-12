#!/usr/bin/env python3
"""Render snpEff database-build and VCF-annotation shell scripts."""

from __future__ import annotations

import argparse
import json
import re
import shlex
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
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'cd "$SCRIPT_DIR"',
        "",
    ]


def write_script(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def snpeff_prefix(args: argparse.Namespace) -> list[str | Raw]:
    if args.snpeff_jar:
        prefix: list[str | Raw] = ["java"]
        if args.java_options:
            prefix.extend(shlex.split(args.java_options))
        prefix.extend(["-jar", args.snpeff_jar])
        return prefix
    return [Raw(args.snpeff_command)]


def snpeff_cmd(args: argparse.Namespace, extra: list[str | Path | Raw]) -> str:
    parts = snpeff_prefix(args)
    if args.config:
        parts.extend(["-c", args.config])
    elif args.use_project_config:
        parts.extend(["-c", "snpEff.project.config"])
    if args.data_dir:
        parts.extend(["-configOption", f"data.dir={args.data_dir}"])
    parts.extend(extra)
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
            f"test -s {q(args.vcf)}",
            f"{snpeff_cmd(args, ['-version'])} > logs/snpeff.version.txt 2>&1 || true",
        ]
    )
    if args.config and not args.use_project_config:
        lines.append(f"test -s {q(args.config)}")
    if custom:
        lines.extend(
            [
                f"test -s {q(args.reference_fasta)}",
                f"test -s {q(args.annotation_file)}",
            ]
        )
        if args.cds_fasta:
            lines.append(f"test -s {q(args.cds_fasta)}")
        if args.protein_fasta:
            lines.append(f"test -s {q(args.protein_fasta)}")
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
            f"ln -sf {q(args.reference_fasta)} \"$SNPEFF_GENOMES_DIR/$SNPEFF_GENOME.fa\"",
            f"ln -sf {q(args.annotation_file)} \"$SNPEFF_GENOME_DIR/{genes_name}\"",
        ]
    )
    if args.cds_fasta:
        lines.append(f"ln -sf {q(args.cds_fasta)} \"$SNPEFF_GENOME_DIR/cds.fa\"")
    if args.protein_fasta:
        lines.append(f"ln -sf {q(args.protein_fasta)} \"$SNPEFF_GENOME_DIR/protein.fa\"")

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
    if args.allow_unchecked_build:
        extra.extend(["-noCheckCds", "-noCheckProtein"])
    extra.append(args.genome_version)
    lines.extend(
        [
            "mkdir -p logs",
            f"{snpeff_cmd(args, extra)} > logs/snpeff.build.log 2>&1",
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
    args.genome_name = args.genome_name or args.genome_version
    return args


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render snpEff annotation scripts")
    parser.add_argument("--vcf", required=True, help="Input VCF/VCF.GZ to annotate")
    parser.add_argument("--genome-version", required=True, help="snpEff database key; must match config/data directory")
    parser.add_argument("--outdir", default="snpeff_annotation", help="Directory for rendered scripts")
    parser.add_argument("--output-vcf", help="Annotated VCF output path inside the rendered script directory")
    parser.add_argument("--snpeff-jar", help="Path to snpEff.jar")
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
    parser.add_argument("--allow-unchecked-build", action="store_true", help="Add -noCheckCds and -noCheckProtein to build")
    parser.add_argument("--compress-output", action="store_true", help="Use bgzip/tabix after annotation when available")
    return normalize_args(parser.parse_args())


def main() -> int:
    args = parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    custom = has_custom_inputs(args)

    scripts: dict[str, list[str]] = {"00.preflight.sh": render_preflight(args, custom)}
    if custom:
        scripts["01.prepare_custom_database.sh"] = render_prepare_custom_database(args)
        scripts["02.build_database.sh"] = render_build_database(args)
    else:
        scripts["02.download_database.sh"] = render_download_database(args)
    scripts["03.annotate_vcf.sh"] = render_annotation(args)

    for name, lines in scripts.items():
        write_script(outdir / name, lines)

    manifest = {
        "genome_version": args.genome_version,
        "custom_build": custom,
        "scripts": list(scripts),
        "outdir": str(outdir),
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
