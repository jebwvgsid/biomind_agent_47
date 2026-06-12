#!/usr/bin/env python3
"""Render WGS GATK/PLINK shell scripts from samples and interval plans."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shlex
from pathlib import Path
from typing import Iterable


class Raw(str):
    """String that should not be shell-quoted because it contains shell syntax."""


def q(value: str | Path | Raw) -> str:
    if isinstance(value, Raw):
        return str(value)
    return shlex.quote(str(value))


def sanitize_token(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return safe or "sample"


def derive_sample_name(bam_path: str) -> str:
    name = Path(bam_path).name
    suffixes = [
        "_mapping.sorted.markdup.bam",
        ".mapping.sorted.markdup.bam",
        "_sorted.markdup.bam",
        ".sorted.markdup.bam",
        ".markdup.bam",
        ".bam",
    ]
    for suffix in suffixes:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def read_samples(path: Path) -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    seen_names: set[str] = set()
    seen_ids: set[str] = set()

    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) == 1:
                bam = parts[0]
                sample = derive_sample_name(bam)
            else:
                sample, bam = parts[0], parts[1]

            if sample in seen_names:
                raise SystemExit(f"{path}:{line_no}: duplicate sample name: {sample}")
            seen_names.add(sample)

            sample_id_base = sanitize_token(sample)
            sample_id = sample_id_base
            index = 2
            while sample_id in seen_ids:
                sample_id = f"{sample_id_base}_{index}"
                index += 1
            seen_ids.add(sample_id)
            samples.append({"sample": sample, "sample_id": sample_id, "bam": bam})

    if not samples:
        raise SystemExit(f"No samples found in {path}")
    return samples


def read_intervals(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"interval_id", "gatk_interval"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"{path}: missing required columns: {', '.join(sorted(missing))}")
        intervals = []
        seen_ids: set[str] = set()
        for line_no, row in enumerate(reader, start=2):
            interval_id = row["interval_id"].strip()
            gatk_interval = row["gatk_interval"].strip()
            if not interval_id or not gatk_interval:
                raise SystemExit(f"{path}:{line_no}: empty interval_id or gatk_interval")
            safe_id = sanitize_token(interval_id)
            if safe_id != interval_id:
                raise SystemExit(f"{path}:{line_no}: interval_id is not path-safe: {interval_id}")
            if interval_id in seen_ids:
                raise SystemExit(f"{path}:{line_no}: duplicate interval_id: {interval_id}")
            seen_ids.add(interval_id)
            intervals.append({"interval_id": interval_id, "gatk_interval": gatk_interval})
    if not intervals:
        raise SystemExit(f"No intervals found in {path}")
    return intervals


def script_header() -> list[str]:
    return [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'cd "$SCRIPT_DIR"',
        "",
    ]


def shell_cmd(container: str | None, singularity_args: list[str], tool: str, args: Iterable[str | Path | Raw]) -> str:
    parts: list[str | Path | Raw] = []
    if container:
        parts.extend(["singularity", "exec"])
        parts.extend(singularity_args)
        parts.append(container)
    parts.append(tool)
    parts.extend(args)
    return " ".join(q(part) for part in parts)


def write_script(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_normalized_samples(outdir: Path, samples: list[dict[str, str]]) -> None:
    with (outdir / "sample_bam.normalized.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=["sample", "sample_id", "bam"])
        writer.writeheader()
        writer.writerows(samples)


def gvcf_path(sample_id: str, interval_id: str, gvcf_root: str) -> str:
    return f"{gvcf_root}/{sample_id}/{sample_id}.{interval_id}.g.vcf.gz"


def raw_vcf_path(interval_id: str, raw_vcf_root: str) -> str:
    return f"{raw_vcf_root}/{interval_id}.raw.vcf.gz"


def render_preflight(args: argparse.Namespace, samples: list[dict[str, str]], intervals: list[dict[str, str]]) -> list[str]:
    lines = script_header()
    lines.extend(
        [
            f"mkdir -p {q(args.tmp_dir)} logs {q(args.gvcf_root)} {q(args.db_root)} {q(args.raw_vcf_root)} filtered plink counts gvcf_maps",
            f"test -s {q(args.ref)}",
            f"if [[ ! -s {q(args.ref + '.fai')} ]]; then echo '[WARN] missing FASTA index: {args.ref}.fai' >&2; fi",
            "",
            "while IFS=$'\\t' read -r sample sample_id bam; do",
            "  [[ \"$sample\" == sample ]] && continue",
            "  test -s \"$bam\"",
            "  if [[ ! -s \"${bam}.bai\" && ! -s \"${bam%.bam}.bai\" ]]; then",
            "    echo \"[WARN] missing BAM index for $sample: $bam\" >&2",
            "  fi",
            "done < sample_bam.normalized.tsv",
            "",
            f"echo '[OK] samples={len(samples)} intervals={len(intervals)}'",
        ]
    )
    return lines


def render_haplotypecaller(args: argparse.Namespace, samples: list[dict[str, str]], intervals: list[dict[str, str]]) -> list[str]:
    lines = script_header()
    gatk = lambda command_args: shell_cmd(args.container, args.singularity_args, "gatk", command_args)
    lines.extend([f"mkdir -p {q(args.tmp_dir)} logs {q(args.gvcf_root)}", ""])
    java = f"-Xmx{args.hc_memory} -Djava.io.tmpdir={args.tmp_dir}"
    for sample in samples:
        sample_id = sample["sample_id"]
        lines.append(f"mkdir -p {q(args.gvcf_root + '/' + sample_id)} {q('logs/' + sample_id)}")
        for interval in intervals:
            interval_id = interval["interval_id"]
            out_gvcf = gvcf_path(sample_id, interval_id, args.gvcf_root)
            log = f"logs/{sample_id}/{sample_id}.{interval_id}.HC.log"
            cmd = gatk(
                [
                    "--java-options",
                    java,
                    "HaplotypeCaller",
                    "-R",
                    args.ref,
                    "-I",
                    sample["bam"],
                    "-L",
                    interval["gatk_interval"],
                    "-ERC",
                    "GVCF",
                    "-O",
                    out_gvcf,
                ]
            )
            lines.append(f"{cmd} > {q(log)} 2>&1")
        lines.append("")
    return lines


def write_gvcf_maps(outdir: Path, args: argparse.Namespace, samples: list[dict[str, str]], intervals: list[dict[str, str]]) -> None:
    map_dir = outdir / "gvcf_maps"
    map_dir.mkdir(parents=True, exist_ok=True)
    for interval in intervals:
        interval_id = interval["interval_id"]
        map_path = map_dir / f"gvcf.{interval_id}.map"
        with map_path.open("w", encoding="utf-8") as handle:
            for sample in samples:
                handle.write(f"{sample['sample']}\t{gvcf_path(sample['sample_id'], interval_id, args.gvcf_root)}\n")


def render_genomicsdb(args: argparse.Namespace, intervals: list[dict[str, str]]) -> list[str]:
    lines = script_header()
    gatk = lambda command_args: shell_cmd(args.container, args.singularity_args, "gatk", command_args)
    lines.extend([f"mkdir -p {q(args.tmp_dir)} logs {q(args.db_root)}", ""])
    java = f"-Xmx{args.db_memory} -Djava.io.tmpdir={args.tmp_dir} -DGATK_STACKTRACE_ON_USER_EXCEPTION=true"
    for interval in intervals:
        interval_id = interval["interval_id"]
        workspace = f"{args.db_root}/genomeDB.{interval_id}"
        log = f"logs/{interval_id}.GenomicsDBImport.log"
        lines.append(f"if [[ -e {q(workspace)} ]]; then echo '[ERROR] GenomicsDB workspace exists: {workspace}' >&2; exit 1; fi")
        cmd = gatk(
            [
                "--java-options",
                java,
                "GenomicsDBImport",
                "--sample-name-map",
                f"gvcf_maps/gvcf.{interval_id}.map",
                "--genomicsdb-workspace-path",
                workspace,
                "-L",
                interval["gatk_interval"],
                "--reader-threads",
                str(args.reader_threads),
                "--batch-size",
                str(args.batch_size),
                "--tmp-dir",
                args.tmp_dir,
            ]
        )
        lines.append(f"{cmd} > {q(log)} 2>&1")
        lines.append("")
    return lines


def render_genotypegvcfs(args: argparse.Namespace, intervals: list[dict[str, str]]) -> list[str]:
    lines = script_header()
    gatk = lambda command_args: shell_cmd(args.container, args.singularity_args, "gatk", command_args)
    lines.extend([f"mkdir -p {q(args.tmp_dir)} logs {q(args.raw_vcf_root)}", ""])
    java = f"-Xmx{args.gt_memory} -Djava.io.tmpdir={args.tmp_dir}"
    for interval in intervals:
        interval_id = interval["interval_id"]
        workspace = f"{args.db_root}/genomeDB.{interval_id}"
        out_vcf = raw_vcf_path(interval_id, args.raw_vcf_root)
        log = f"logs/{interval_id}.GenotypeGVCFs.log"
        cmd = gatk(
            [
                "--java-options",
                java,
                "GenotypeGVCFs",
                "-R",
                args.ref,
                "-V",
                Raw(f"gendb://$PWD/{workspace}"),
                "-O",
                out_vcf,
            ]
        )
        lines.append(f"{cmd} > {q(log)} 2>&1")
    return lines


def write_raw_vcf_list(outdir: Path, args: argparse.Namespace, intervals: list[dict[str, str]]) -> None:
    with (outdir / "raw_vcf.list").open("w", encoding="utf-8") as handle:
        for interval in intervals:
            handle.write(raw_vcf_path(interval["interval_id"], args.raw_vcf_root) + "\n")


def render_merge(args: argparse.Namespace) -> list[str]:
    lines = script_header()
    gatk = lambda command_args: shell_cmd(args.container, args.singularity_args, "gatk", command_args)
    java = f"-Xmx{args.merge_memory} -Djava.io.tmpdir={args.tmp_dir}"
    lines.extend(
        [
            f"mkdir -p {q(args.tmp_dir)} logs",
            f"{gatk(['--java-options', java, 'MergeVcfs', '-I', 'raw_vcf.list', '-O', 'all.merge_raw.vcf.gz'])} > logs/MergeVcfs.log 2>&1",
            f"{gatk(['IndexFeatureFile', '-I', 'all.merge_raw.vcf.gz'])} > logs/MergeVcfs.index.log 2>&1",
        ]
    )
    return lines


def render_filter(args: argparse.Namespace) -> list[str]:
    lines = script_header()
    gatk = lambda command_args: shell_cmd(args.container, args.singularity_args, "gatk", command_args)
    java = f"-Xmx{args.filter_memory} -Djava.io.tmpdir={args.tmp_dir}"
    lines.extend([f"mkdir -p {q(args.tmp_dir)} logs filtered", ""])
    lines.append(
        gatk(
            [
                "--java-options",
                java,
                "SelectVariants",
                "-R",
                args.ref,
                "-V",
                "all.merge_raw.vcf.gz",
                "--select-type",
                "SNP",
                "-O",
                "filtered/gatk.snp.vcf.gz",
            ]
        )
        + " > logs/SelectVariants.snp.log 2>&1"
    )
    filter_args = [
        "--java-options",
        java,
        "VariantFiltration",
        "-R",
        args.ref,
        "-V",
        "filtered/gatk.snp.vcf.gz",
        "-filter",
        "QD < 2.0",
        "--filter-name",
        "QD2",
        "-filter",
        "QUAL < 30.0",
        "--filter-name",
        "QUAL30",
        "-filter",
        "SOR > 3.0",
        "--filter-name",
        "SOR3",
        "-filter",
        "FS > 60.0",
        "--filter-name",
        "FS60",
        "-filter",
        "MQ < 40.0",
        "--filter-name",
        "MQ40",
        "-filter",
        "MQRankSum < -12.5",
        "--filter-name",
        "MQRankSum-12.5",
        "-filter",
        "ReadPosRankSum < -8.0",
        "--filter-name",
        "ReadPosRankSum-8",
        "-O",
        "filtered/gatk.filter.snp.vcf.gz",
    ]
    lines.append(gatk(filter_args) + " > logs/VariantFiltration.snp.log 2>&1")
    lines.append(
        gatk(
            [
                "--java-options",
                java,
                "SelectVariants",
                "-R",
                args.ref,
                "-V",
                "filtered/gatk.filter.snp.vcf.gz",
                "--exclude-filtered",
                "-O",
                "filtered/gatk.filtered.snp.vcf.gz",
            ]
        )
        + " > logs/SelectVariants.pass_snp.log 2>&1"
    )
    return lines


def render_plink(args: argparse.Namespace) -> list[str]:
    lines = script_header()
    plink = lambda command_args: shell_cmd(args.container, args.singularity_args, "plink", command_args)
    lines.extend([f"mkdir -p plink logs", ""])
    lines.append(
        plink(
            [
                "--vcf",
                "filtered/gatk.filtered.snp.vcf.gz",
                "--geno",
                str(args.plink_geno),
                "--maf",
                str(args.plink_maf),
                "--biallelic-only",
                "strict",
                "--out",
                "plink/all.missing_maf",
                "--recode",
                "vcf-iid",
                "--allow-extra-chr",
                "--set-missing-var-ids",
                "@:#",
                "--keep-allele-order",
            ]
        )
        + " > logs/plink.missing_maf.log 2>&1"
    )
    lines.append(
        plink(
            [
                "--vcf",
                "plink/all.missing_maf.vcf",
                "--indep-pairwise",
                str(args.ld_window_kb),
                str(args.ld_step),
                str(args.ld_r2),
                "--out",
                "plink/tmp.ld",
                "--allow-extra-chr",
                "--set-missing-var-ids",
                "@:#",
            ]
        )
        + " > logs/plink.ld_prune_sites.log 2>&1"
    )
    lines.append(
        plink(
            [
                "--vcf",
                "plink/all.missing_maf.vcf",
                "--extract",
                "plink/tmp.ld.prune.in",
                "--out",
                "plink/all.LDfilter",
                "--recode",
                "vcf-iid",
                "--keep-allele-order",
                "--allow-extra-chr",
                "--set-missing-var-ids",
                "@:#",
            ]
        )
        + " > logs/plink.ld_filter.log 2>&1"
    )
    lines.append(
        plink(
            [
                "--vcf",
                "plink/all.LDfilter.vcf",
                "--pca",
                str(args.pca_components),
                "--out",
                "plink/PCA_plink.out",
                "--allow-extra-chr",
                "--set-missing-var-ids",
                "@:#",
            ]
        )
        + " > logs/plink.pca.log 2>&1"
    )
    return lines


def render_counts(args: argparse.Namespace) -> list[str]:
    lines = script_header()
    bcftools = shell_cmd(args.container, args.singularity_args, "bcftools", [])
    vcf = "filtered/gatk.filtered.snp.vcf.gz"
    awk_script = (
        "BEGIN{while((getline name<\"counts/sample.names\")>0)n[++k]=name; print \"sample\\tnon_ref_snp_count\"} "
        "{for(i=3;i<=NF;i++){split($i,a,\":\"); gt=a[1]; "
        "if(gt!=\".\" && gt!=\"./.\" && gt!=\".|.\" && gt!=\"0/0\" && gt!=\"0|0\" && gt!=\"0\") c[i-2]++}} "
        "END{for(i=1;i<=k;i++) print n[i]\"\\t\"0+c[i]}"
    )
    lines.extend(
        [
            "mkdir -p counts logs",
            f"{bcftools} view -H -v snps {q(vcf)} | wc -l > counts/total_snp_count.txt",
            f"{bcftools} query -l {q(vcf)} > counts/sample.names",
            f"{bcftools} query -f '%CHROM\\t%POS[\\t%GT]\\n' {q(vcf)} | awk {q(awk_script)} > counts/snp_count_per_sample.tsv",
        ]
    )
    return lines


def render_annotation_template(args: argparse.Namespace) -> list[str]:
    lines = script_header()
    tool = lambda name, command_args: shell_cmd(args.container, args.singularity_args, name, command_args)
    vcf = args.annotation_vcf
    dbtype = args.annovar_dbtype
    dbdir = args.annovar_dbdir
    if args.annotation_gff:
        gff_setup = f"ANNOTATION_GFF3={q(args.annotation_gff)}"
    else:
        gff_setup = ': "${ANNOTATION_GFF3:?set ANNOTATION_GFF3 or pass --annotation-gff}"'
    if args.annotation_fasta:
        fasta_setup = f"ANNOTATION_FASTA={q(args.annotation_fasta)}"
    else:
        fasta_setup = ': "${ANNOTATION_FASTA:?set ANNOTATION_FASTA or pass --annotation-fasta}"'
    if args.annovar_buildver:
        buildver_setup = f"ANNOVAR_BUILDVER={q(args.annovar_buildver)}"
    else:
        buildver_setup = ': "${ANNOVAR_BUILDVER:?set ANNOVAR_BUILDVER or pass --annovar-buildver for this project/database}"'
    lines.extend(
        [
            "mkdir -p logs",
            gff_setup,
            fasta_setup,
            buildver_setup,
            f"ANNOVAR_DBTYPE={q(dbtype)}",
            f"ANNOVAR_DBDIR={q(dbdir)}",
            'mkdir -p "$ANNOVAR_DBDIR"',
            f"{tool('gff3ToGenePred', [Raw('\"$ANNOTATION_GFF3\"'), Raw('\"$ANNOVAR_DBDIR/${ANNOVAR_BUILDVER}_${ANNOVAR_DBTYPE}.txt\"')])} > logs/annovar.gff3ToGenePred.log 2>&1",
            f"{tool('retrieve_seq_from_fasta.pl', ['--format', 'refGene', '--seqfile', Raw('\"$ANNOTATION_FASTA\"'), '--out', Raw('\"$ANNOVAR_DBDIR/${ANNOVAR_BUILDVER}_${ANNOVAR_DBTYPE}Mrna.fa\"'), Raw('\"$ANNOVAR_DBDIR/${ANNOVAR_BUILDVER}_${ANNOVAR_DBTYPE}.txt\"')])} > logs/annovar.retrieve_seq.log 2>&1",
            f"{tool('convert2annovar.pl', ['-format', 'vcf4', '-allsample', '-withfreq', vcf])} > \"$ANNOVAR_DBDIR/all.snp.vcf.annovar.input\" 2> logs/annovar.convert2annovar.log",
            f"{tool('annotate_variation.pl', ['-geneanno', '--neargene', '2000', '-buildver', Raw('\"$ANNOVAR_BUILDVER\"'), '-dbtype', Raw('\"$ANNOVAR_DBTYPE\"'), '-outfile', Raw('\"$ANNOVAR_DBDIR/all.anno\"'), '-exonsort', Raw('\"$ANNOVAR_DBDIR/all.snp.vcf.annovar.input\"'), Raw('\"$ANNOVAR_DBDIR\"')])} > logs/annovar.annotate_variation.log 2>&1",
        ]
    )
    return lines


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render WGS GATK shell scripts")
    parser.add_argument("--sample-bam-list", required=True, help="Two-column sample/BAM list, or one BAM path per line")
    parser.add_argument("--intervals-tsv", required=True, help="Output from make_gatk_interval_plan.py")
    parser.add_argument("--ref", required=True, help="Reference FASTA path visible where scripts will run")
    parser.add_argument("--container", help="Optional Singularity/Apptainer image containing gatk, plink, bcftools")
    parser.add_argument("--singularity-args", default="", help="Extra args inserted after 'singularity exec'")
    parser.add_argument("--outdir", default="wgs_gatk_scripts", help="Directory for rendered scripts")
    parser.add_argument("--tmp-dir", default="tmp")
    parser.add_argument("--gvcf-root", default="gvcf")
    parser.add_argument("--db-root", default="genomicsdb")
    parser.add_argument("--raw-vcf-root", default="raw_vcf")
    parser.add_argument("--hc-memory", default="10g")
    parser.add_argument("--db-memory", default="10g")
    parser.add_argument("--gt-memory", default="10g")
    parser.add_argument("--merge-memory", default="10g")
    parser.add_argument("--filter-memory", default="4g")
    parser.add_argument("--reader-threads", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--plink-geno", type=float, default=0.2)
    parser.add_argument("--plink-maf", type=float, default=0.05)
    parser.add_argument("--ld-window-kb", type=int, default=50)
    parser.add_argument("--ld-step", type=int, default=10)
    parser.add_argument("--ld-r2", type=float, default=0.5)
    parser.add_argument("--pca-components", type=int, default=10)
    parser.add_argument("--annotation-gff", help="Optional GFF3 for ANNOVAR-style annotation script")
    parser.add_argument("--annotation-fasta", help="Optional FASTA for ANNOVAR-style annotation script")
    parser.add_argument("--annotation-vcf", default="filtered/gatk.filtered.snp.vcf.gz")
    parser.add_argument("--annovar-buildver", help="Project/database buildver prefix for ANNOVAR files")
    parser.add_argument("--annovar-dbtype", default="refGene", help="ANNOVAR database type; use refGene for gff3ToGenePred-derived databases")
    parser.add_argument("--annovar-dbdir", default="annotation", help="ANNOVAR database/output directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.singularity_args = shlex.split(args.singularity_args)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    samples = read_samples(Path(args.sample_bam_list))
    intervals = read_intervals(Path(args.intervals_tsv))
    write_normalized_samples(outdir, samples)
    write_gvcf_maps(outdir, args, samples, intervals)
    write_raw_vcf_list(outdir, args, intervals)

    scripts = {
        "00.preflight.sh": render_preflight(args, samples, intervals),
        "step1.HaplotypeCaller.sh": render_haplotypecaller(args, samples, intervals),
        "step2.GenomicsDBImport.sh": render_genomicsdb(args, intervals),
        "step3.GenotypeGVCFs.sh": render_genotypegvcfs(args, intervals),
        "step4.MergeVcfs.sh": render_merge(args),
        "step5.FilterSNPs.sh": render_filter(args),
        "step6.Plink_LD_PCA.sh": render_plink(args),
        "step7.SNP_counts.sh": render_counts(args),
        "step8.ANNOVAR_annotation.sh": render_annotation_template(args),
    }
    for name, lines in scripts.items():
        write_script(outdir / name, lines)

    manifest = {
        "samples": len(samples),
        "intervals": len(intervals),
        "scripts": list(scripts),
        "outdir": str(outdir),
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
