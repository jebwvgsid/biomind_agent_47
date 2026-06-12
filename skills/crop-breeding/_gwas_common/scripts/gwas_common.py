#!/usr/bin/env python3
"""Shared helpers for BioMind crop GWAS wrappers."""

from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


P_VALUE_ALIASES = ("p", "pval", "pvalue", "p_value", "p.value", "p_wald", "p_lrt", "p_score")
CHR_ALIASES = ("chr", "chrom", "chromosome", "chromosome_id", "seqid")
POS_ALIASES = ("pos", "bp", "position", "base_pair", "start")
ID_ALIASES = ("marker_id", "id", "snp", "snp_id", "marker", "variant", "variant_id")
SAMPLE_ALIASES = ("sample_id", "sample", "taxa", "taxon", "iid", "id", "line", "accession")
EFFECT_ALIASES = ("beta", "effect", "estimate", "effect_size", "b")


def read_table(path: str | Path) -> List[Dict[str, str]]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        delimiter = "\t" if path.suffix.lower() in {".tsv", ".txt"} else ","
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters="\t,;")
            delimiter = dialect.delimiter
        except csv.Error:
            pass
        reader = csv.DictReader(handle, delimiter=delimiter)
        return [{str(k): ("" if v is None else str(v)) for k, v in row.items()} for row in reader]


def write_tsv(path: str | Path, rows: Iterable[Dict[str, object]], fieldnames: Sequence[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def detect_column(
    headers: Sequence[str],
    explicit: Optional[str],
    aliases: Sequence[str],
    label: str,
    required: bool = True,
) -> Optional[str]:
    if explicit:
        if explicit in headers:
            return explicit
        raise ValueError(f"{label} column '{explicit}' is not present. Available columns: {', '.join(headers)}")
    lower = {header.lower(): header for header in headers}
    for alias in aliases:
        if alias.lower() in lower:
            return lower[alias.lower()]
    if required:
        raise ValueError(f"Could not detect {label} column. Available columns: {', '.join(headers)}")
    return None


def safe_float(value: object) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return math.nan
    return parsed if math.isfinite(parsed) else math.nan


def safe_int(value: object) -> Optional[int]:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def sanitize_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return clean.strip("._") or "trait"


def ensure_dir(path: str | Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_command(log_path: str | Path, command: Sequence[str] | str, cwd: str | Path | None = None) -> None:
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    text = command if isinstance(command, str) else " ".join(shell_quote(part) for part in command)
    if cwd:
        text = f"(cd {shell_quote(str(cwd))} && {text})"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


def shell_quote(value: str) -> str:
    value = str(value)
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./\\-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def run_command(
    command: Sequence[str],
    commands_log: str | Path,
    cwd: str | Path | None = None,
    dry_run: bool = False,
) -> None:
    append_command(commands_log, command, cwd=cwd)
    if dry_run:
        return
    executable = command[0]
    if shutil.which(executable) is None:
        raise RuntimeError(f"Required executable is not on PATH: {executable}")
    subprocess.run(list(command), cwd=str(cwd) if cwd else None, check=True)


def read_phenotype_samples(path: str | Path, sample_col: Optional[str] = None) -> List[str]:
    rows = read_table(path)
    if not rows:
        return []
    headers = list(rows[0].keys())
    column = detect_column(headers, sample_col, SAMPLE_ALIASES, "sample", required=True)
    return [row[column].strip() for row in rows if row.get(column, "").strip()]


def read_vcf_samples(path: str | Path) -> List[str]:
    opener = open
    if str(path).endswith(".gz"):
        import gzip

        opener = gzip.open
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("#CHROM"):
                parts = line.rstrip("\n").split("\t")
                return parts[9:] if len(parts) > 9 else []
    return []


def read_psam_samples(prefix: str | Path) -> List[str]:
    path = Path(str(prefix) + ".psam")
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        header = handle.readline().strip().split()
        if not header:
            return []
        try:
            iid_index = [item.lstrip("#") for item in header].index("IID")
        except ValueError:
            iid_index = min(1, len(header) - 1)
        samples = []
        for line in handle:
            parts = line.strip().split()
            if len(parts) > iid_index:
                samples.append(parts[iid_index])
        return samples


def read_fam_samples(prefix: str | Path) -> List[str]:
    path = Path(str(prefix) + ".fam")
    if not path.is_file():
        return []
    samples = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.strip().split()
            if len(parts) >= 2:
                samples.append(parts[1])
    return samples


def write_sample_overlap(
    genotype_samples: Sequence[str],
    phenotype_samples: Sequence[str],
    outdir: str | Path,
) -> Dict[str, int]:
    outdir = ensure_dir(outdir)
    genotype_set = set(genotype_samples)
    phenotype_set = set(phenotype_samples)
    rows = []
    for sample in sorted(genotype_set | phenotype_set):
        rows.append(
            {
                "sample_id": sample,
                "in_genotype": int(sample in genotype_set),
                "in_phenotype": int(sample in phenotype_set),
                "in_both": int(sample in genotype_set and sample in phenotype_set),
            }
        )
    write_tsv(outdir / "sample_overlap.tsv", rows, ["sample_id", "in_genotype", "in_phenotype", "in_both"])
    return {
        "genotype_samples": len(genotype_set),
        "phenotype_samples": len(phenotype_set),
        "overlap_samples": len(genotype_set & phenotype_set),
    }


def standardize_summary(
    gwas_path: str | Path,
    outdir: str | Path,
    trait: str,
    method: str,
    p_col: Optional[str] = None,
    chr_col: Optional[str] = None,
    pos_col: Optional[str] = None,
    id_col: Optional[str] = None,
    effect_col: Optional[str] = None,
    source: str = "",
) -> Dict[str, object]:
    rows = read_table(gwas_path)
    outdir = ensure_dir(outdir)
    figures = ensure_dir(outdir / "figures")
    if not rows:
        headers = ["marker_id", "chr", "pos", "p_value", "trait", "method", "effect"]
        write_tsv(outdir / "summary_stats.tsv", [], headers)
        write_tsv(outdir / "lead_snps.tsv", [], headers + ["threshold"])
        write_tsv(outdir / "lambda_gc.tsv", [{"metric": "lambda_gc", "value": "", "method": "no_data"}], ["metric", "value", "method"])
        return {"n_variants": 0, "lead_snps": 0, "summary_stats": str(outdir / "summary_stats.tsv")}

    headers = list(rows[0].keys())
    p_name = detect_column(headers, p_col, P_VALUE_ALIASES, "p-value", required=True)
    chr_name = detect_column(headers, chr_col, CHR_ALIASES, "chromosome", required=True)
    pos_name = detect_column(headers, pos_col, POS_ALIASES, "position", required=True)
    id_name = detect_column(headers, id_col, ID_ALIASES, "marker ID", required=False)
    effect_name = detect_column(headers, effect_col, EFFECT_ALIASES, "effect", required=False)

    standardized = []
    p_values = []
    for index, row in enumerate(rows, start=1):
        p_value = safe_float(row.get(p_name))
        pos = safe_int(row.get(pos_name))
        chrom = str(row.get(chr_name, "")).strip()
        marker = str(row.get(id_name, "")).strip() if id_name else ""
        if not marker:
            marker = f"{chrom}:{pos if pos is not None else index}"
        effect = row.get(effect_name, "") if effect_name else ""
        standardized.append(
            {
                "marker_id": marker,
                "chr": chrom,
                "pos": "" if pos is None else pos,
                "p_value": "" if math.isnan(p_value) else f"{p_value:.12g}",
                "trait": trait,
                "method": method,
                "effect": effect,
                "source": source,
            }
        )
        if not math.isnan(p_value) and 0 < p_value <= 1:
            p_values.append(p_value)

    n = len(p_values)
    genome_sig = 0.05 / max(n, 1)
    suggestive = 1.0 / max(n, 1)
    leads = []
    for row in standardized:
        p_value = safe_float(row["p_value"])
        if not math.isnan(p_value) and p_value <= suggestive:
            lead = dict(row)
            lead["threshold"] = "suggestive"
            if p_value <= genome_sig:
                lead["threshold"] = "bonferroni_0.05"
            leads.append(lead)
    leads.sort(key=lambda item: safe_float(item["p_value"]))

    fields = ["marker_id", "chr", "pos", "p_value", "trait", "method", "effect", "source"]
    write_tsv(outdir / "summary_stats.tsv", standardized, fields)
    write_tsv(outdir / "lead_snps.tsv", leads, fields + ["threshold"])
    lambda_gc, lambda_method = estimate_lambda_gc(p_values)
    write_tsv(
        outdir / "lambda_gc.tsv",
        [{"metric": "lambda_gc", "value": "" if math.isnan(lambda_gc) else f"{lambda_gc:.6g}", "method": lambda_method}],
        ["metric", "value", "method"],
    )
    maybe_write_plots(standardized, figures, trait)
    result = {
        "n_variants": n,
        "genome_sig": genome_sig,
        "suggestive": suggestive,
        "lead_snps": len(leads),
        "summary_stats": str(outdir / "summary_stats.tsv"),
        "lead_snps_path": str(outdir / "lead_snps.tsv"),
        "lambda_gc_path": str(outdir / "lambda_gc.tsv"),
    }
    write_json(outdir / "result.json", result)
    write_report(
        outdir / "report.md",
        [
            f"# Crop GWAS {trait}",
            "",
            f"- method: {method}",
            f"- source: {source or gwas_path}",
            f"- variants with valid p-values: {n}",
            f"- suggestive threshold: {suggestive:.6g}",
            f"- Bonferroni 0.05 threshold: {genome_sig:.6g}",
            f"- lead SNPs: {len(leads)}",
            f"- lambda GC: {'' if math.isnan(lambda_gc) else f'{lambda_gc:.6g}'} ({lambda_method})",
        ],
    )
    return result


def estimate_lambda_gc(p_values: Sequence[float]) -> Tuple[float, str]:
    clean = [p for p in p_values if 0 < p <= 1]
    if not clean:
        return math.nan, "no_valid_p_values"
    try:
        from scipy.stats import chi2

        chisq = [float(chi2.isf(p, 1)) for p in clean]
        return median(chisq) / 0.454936423119572, "chi2_df1_scipy"
    except Exception:
        proxies = [-2.0 * math.log(max(p, 1e-300)) for p in clean]
        return median(proxies) / 1.3862943611198906, "minus2logp_proxy"


def median(values: Sequence[float]) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def maybe_write_plots(rows: Sequence[Dict[str, object]], figures: Path, trait: str) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    clean = []
    for index, row in enumerate(rows, start=1):
        p_value = safe_float(row.get("p_value"))
        if math.isnan(p_value) or p_value <= 0:
            continue
        clean.append((index, str(row.get("chr", "")), -math.log10(p_value)))
    if not clean:
        return
    xs = [item[0] for item in clean]
    ys = [item[2] for item in clean]
    chroms = [item[1] for item in clean]
    chrom_order = {chrom: idx for idx, chrom in enumerate(dict.fromkeys(chroms))}
    colors = ["#2d6a8e" if chrom_order[chrom] % 2 == 0 else "#b44b4b" for chrom in chroms]
    plt.figure(figsize=(10, 4))
    plt.scatter(xs, ys, c=colors, s=8)
    plt.xlabel("Genome")
    plt.ylabel("-log10(P)")
    plt.title(f"{trait} GWAS")
    plt.tight_layout()
    plt.savefig(figures / f"{sanitize_name(trait)}.manhattan.png", dpi=180)
    plt.close()
    observed = sorted([safe_float(row.get("p_value")) for row in rows if not math.isnan(safe_float(row.get("p_value"))) and safe_float(row.get("p_value")) > 0])
    expected = [(i + 1) / (len(observed) + 1) for i in range(len(observed))]
    plt.figure(figsize=(5, 5))
    plt.scatter([-math.log10(p) for p in expected], [-math.log10(p) for p in observed], s=8)
    limit = max(max(-math.log10(p) for p in expected), max(-math.log10(p) for p in observed))
    plt.plot([0, limit], [0, limit], color="grey")
    plt.xlabel("Expected -log10(P)")
    plt.ylabel("Observed -log10(P)")
    plt.title(f"{trait} QQ")
    plt.tight_layout()
    plt.savefig(figures / f"{sanitize_name(trait)}.qq.png", dpi=180)
    plt.close()


def parse_gff_genes(gff_path: str | Path) -> List[Dict[str, object]]:
    genes = []
    with Path(gff_path).open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 9 or parts[2] != "gene":
                continue
            start = safe_int(parts[3])
            end = safe_int(parts[4])
            if start is None or end is None:
                continue
            attrs = parts[8]
            gene_id = attr_value(attrs, "ID") or attr_value(attrs, "Name") or f"{parts[0]}:{start}-{end}"
            genes.append({"chr": parts[0], "start": start, "end": end, "gene_id": gene_id, "attributes": attrs})
    return genes


def attr_value(attrs: str, key: str) -> str:
    match = re.search(rf"(?:^|;){re.escape(key)}=([^;]+)", attrs)
    return match.group(1) if match else ""


def map_snps_to_genes(
    snps_path: str | Path,
    gff_path: str | Path,
    outdir: str | Path,
    window: int = 100000,
    chr_col: Optional[str] = None,
    pos_col: Optional[str] = None,
    id_col: Optional[str] = None,
) -> Dict[str, object]:
    snps = read_table(snps_path)
    genes = parse_gff_genes(gff_path)
    outdir = ensure_dir(outdir)
    if not snps:
        write_tsv(outdir / "lead_snp_nearest_genes.tsv", [], ["marker_id", "chr", "pos", "gene_id", "distance"])
        write_tsv(outdir / "snp_window_genes.tsv", [], ["marker_id", "chr", "pos", "gene_id", "gene_start", "gene_end", "window_bp"])
        write_tsv(outdir / "candidate_genes.tsv", [], ["gene_id", "chr", "gene_start", "gene_end", "marker_count"])
        return {"snps": 0, "candidate_genes": 0}
    headers = list(snps[0].keys())
    chr_name = detect_column(headers, chr_col, CHR_ALIASES, "chromosome", required=True)
    pos_name = detect_column(headers, pos_col, POS_ALIASES, "position", required=True)
    id_name = detect_column(headers, id_col, ID_ALIASES, "marker ID", required=False)
    by_chr: Dict[str, List[Dict[str, object]]] = {}
    for gene in genes:
        by_chr.setdefault(str(gene["chr"]), []).append(gene)
    nearest_rows = []
    window_rows = []
    candidate_counts: Dict[str, Dict[str, object]] = {}
    for index, snp in enumerate(snps, start=1):
        chrom = str(snp.get(chr_name, "")).strip()
        pos = safe_int(snp.get(pos_name))
        if pos is None:
            continue
        marker = str(snp.get(id_name, "")).strip() if id_name else ""
        if not marker:
            marker = f"{chrom}:{pos}"
        chr_genes = by_chr.get(chrom, [])
        if not chr_genes:
            continue
        ranked = sorted(chr_genes, key=lambda gene: gene_distance(pos, int(gene["start"]), int(gene["end"])))
        nearest = ranked[0]
        nearest_rows.append(
            {
                "marker_id": marker,
                "chr": chrom,
                "pos": pos,
                "gene_id": nearest["gene_id"],
                "gene_start": nearest["start"],
                "gene_end": nearest["end"],
                "distance": gene_distance(pos, int(nearest["start"]), int(nearest["end"])),
            }
        )
        for gene in chr_genes:
            if int(gene["end"]) >= pos - window and int(gene["start"]) <= pos + window:
                row = {
                    "marker_id": marker,
                    "chr": chrom,
                    "pos": pos,
                    "gene_id": gene["gene_id"],
                    "gene_start": gene["start"],
                    "gene_end": gene["end"],
                    "window_bp": window,
                }
                window_rows.append(row)
                record = candidate_counts.setdefault(
                    str(gene["gene_id"]),
                    {"gene_id": gene["gene_id"], "chr": chrom, "gene_start": gene["start"], "gene_end": gene["end"], "marker_count": 0},
                )
                record["marker_count"] = int(record["marker_count"]) + 1
    write_tsv(outdir / "lead_snp_nearest_genes.tsv", nearest_rows, ["marker_id", "chr", "pos", "gene_id", "gene_start", "gene_end", "distance"])
    write_tsv(outdir / "snp_window_genes.tsv", window_rows, ["marker_id", "chr", "pos", "gene_id", "gene_start", "gene_end", "window_bp"])
    candidates = sorted(candidate_counts.values(), key=lambda item: (str(item["chr"]), int(item["gene_start"]), str(item["gene_id"])))
    write_tsv(outdir / "candidate_genes.tsv", candidates, ["gene_id", "chr", "gene_start", "gene_end", "marker_count"])
    return {"snps": len(snps), "candidate_genes": len(candidates)}


def gene_distance(pos: int, start: int, end: int) -> int:
    if start <= pos <= end:
        return 0
    return min(abs(pos - start), abs(pos - end))


def write_json(path: str | Path, payload: Dict[str, object]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_report(path: str | Path, lines: Sequence[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def repo_root_from_script(script_path: str | Path) -> Path:
    path = Path(script_path).resolve()
    for parent in path.parents:
        if (parent / "skills").is_dir() and (parent / "agent_core").is_dir():
            return parent
    return path.parents[3]


def prepend_common_path(script_file: str) -> None:
    # Kept for wrappers that import this module through copied scripts.
    os.environ.setdefault("BIOMIND_GWAS_COMMON", str(Path(script_file).resolve().parent))
