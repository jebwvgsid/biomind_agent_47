#!/usr/bin/env python3
"""Build extended public BioMind fixtures from cached raw downloads.

This script expects raw public data to already be cached under
`.biomind/public_raw`:

- clim4omics/File Control.zip and clim4omics/Control.zip
- maize_pangenome/PopSV.zip
- maize_pan_transcriptome/maize_503genotypes_raw_expression_values_FPKM.txt
- metabolomics_workbench/ST001947.mwtab.txt
- giab_hg002/HG002_GRCh38_1_22_v4.2.1_benchmark.vcf.gz
- giab_hg002/HG002_GRCh38_1_22_v4.2.1_benchmark_noinconsistent.bed

It writes small benchmark-ready subsets under `fixtures_public`.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd


BENCHMARK_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BENCHMARK_DIR.parents[1]
DEFAULT_RAWDIR = REPO_ROOT / ".biomind" / "public_raw"
DEFAULT_OUTDIR = BENCHMARK_DIR / "fixtures_public"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")


def build_g2f_gxe(rawdir: Path, outdir: Path) -> None:
    raw = rawdir / "clim4omics"
    out = ensure_dir(outdir / "g2f_maize_gxe")

    with ZipFile(raw / "File Control.zip") as archive:
        pheno = pd.read_csv(archive.open("File Control/Phenotype/output/g2f_2021_phenotypic_clean_data.csv"))
        weather = pd.read_csv(archive.open("File Control/Environment/output/g2f_2021_weather_cleaned.csv"))

    with ZipFile(raw / "Control.zip") as archive:
        genotype = pd.read_csv(archive.open("Control/output/X.csv"), usecols=range(501))

    genotype = genotype.rename(columns={genotype.columns[0]: "parent_id"}).set_index("parent_id")
    markers = list(genotype.columns[:500])
    parent_set = set(genotype.index.astype(str))

    pheno["sample_id"] = pheno["Pedigree.x"].astype(str)
    pheno["environment_id"] = pheno["Year.x"].astype(str) + "_" + pheno["Experiment"].astype(str)
    pheno["trait_yield"] = pd.to_numeric(pheno["Grain Yield [bu/A]"], errors="coerce")
    pheno["P1"] = pheno["P1"].astype(str)
    pheno["P2"] = pheno["P2"].astype(str)
    pheno = pheno[
        pheno["trait_yield"].notna()
        & pheno["P1"].isin(parent_set)
        & pheno["P2"].isin(parent_set)
    ].copy()

    counts = pheno.groupby("sample_id")["environment_id"].nunique()
    keep_samples = counts[counts >= 2].index[:80]
    pheno = pheno[pheno["sample_id"].isin(keep_samples)].copy()
    if pheno.empty:
        raise RuntimeError("No repeated hybrids with both parents found in CLIM4OMICS genotype matrix")

    phenotype = pheno[["sample_id", "environment_id", "trait_yield", "Year.x", "Experiment", "P1", "P2"]].rename(
        columns={"Year.x": "year", "Experiment": "location"}
    ).drop_duplicates()
    phenotype.to_csv(out / "phenotype_multi_environment.tsv", sep="\t", index=False)

    geno_rows: list[dict[str, object]] = []
    for sample_id, row in pheno.drop_duplicates("sample_id").set_index("sample_id").iterrows():
        p1 = genotype.loc[row["P1"], markers].apply(pd.to_numeric, errors="coerce")
        p2 = genotype.loc[row["P2"], markers].apply(pd.to_numeric, errors="coerce")
        values = ((p1 + p2) / 2).to_dict()
        values["sample_id"] = sample_id
        geno_rows.append(values)
    pd.DataFrame(geno_rows)[["sample_id", *markers]].to_csv(out / "genotype_matrix.tsv", sep="\t", index=False)

    weather["environment_id"] = weather["Year [Local]"].astype(str) + "_" + weather["Experiment"].astype(str)
    weather["date"] = pd.to_datetime(weather["Date"], errors="coerce")
    weather["day"] = weather["date"].dt.dayofyear
    vars_map = {
        "Temperature [C]": "Temperature_C",
        "Rainfall [mm]": "Rainfall_mm",
        "Relative Humidity [%]": "Relative_Humidity_pct",
        "Solar Radiation [W/m2]": "Solar_Radiation_W_m2",
    }
    weather = weather[weather["environment_id"].isin(set(phenotype["environment_id"]))].copy()
    long_rows = []
    for source_column, variable in vars_map.items():
        if source_column not in weather.columns:
            continue
        tmp = weather[["environment_id", "date", "day", source_column]].rename(columns={source_column: "value"})
        tmp["variable"] = variable
        long_rows.append(tmp)
    env = pd.concat(long_rows, ignore_index=True).dropna(subset=["day", "value"])
    env[["environment_id", "date", "day", "variable", "value"]].to_csv(out / "environment_timeseries.tsv", sep="\t", index=False)

    pd.DataFrame([{
        "stage": "flowering_grain_fill_demo",
        "start_day": 150,
        "end_day": 230,
        "variable": "Temperature_C",
    }]).to_csv(out / "growth_stage_window.tsv", sep="\t", index=False)

    files = [
        "phenotype_multi_environment.tsv",
        "genotype_matrix.tsv",
        "environment_timeseries.tsv",
        "growth_stage_window.tsv",
    ]
    answer = [
        "dataset_id: public-clim4omics-g2f-2021-gxe-subset",
        "source: Zenodo 8002909 CLIM4OMICS / G2F 2021",
        f"n_samples: {phenotype['sample_id'].nunique()}",
        f"n_phenotype_rows: {len(phenotype)}",
        f"n_environments: {phenotype['environment_id'].nunique()}",
        f"n_markers: {len(markers)}",
        "files:",
    ]
    answer.extend(f"  {name}: {sha256_file(out / name)}" for name in files)
    write_text(out / "answer_key.yaml", "\n".join(answer) + "\n")


def build_sv_and_epav(rawdir: Path, outdir: Path) -> None:
    out = ensure_dir(outdir / "maize_pangenome_sv")
    popsv = rawdir / "maize_pangenome" / "PopSV.zip"
    with ZipFile(popsv) as archive:
        with archive.open("PopSV/all_chr.paragraph-SV.350.minmaf0.05_maxmissing0.6.bi.vcf.gz") as compressed:
            with gzip.open(compressed, "rt", encoding="utf-8") as src, (out / "sv.vcf").open("w", encoding="utf-8") as dst:
                records = 0
                for line in src:
                    if line.startswith("#"):
                        dst.write(line)
                    elif records < 500:
                        dst.write(line)
                        records += 1
                    else:
                        break

    expression_path = rawdir / "maize_pan_transcriptome" / "maize_503genotypes_raw_expression_values_FPKM.txt"
    expression = pd.read_csv(expression_path, sep=None, engine="python")
    gene_col = expression.columns[0]
    probe = expression.head(200)
    numeric_cols = [
        column for column in expression.columns[1:]
        if pd.to_numeric(probe[column], errors="coerce").notna().mean() >= 0.8
    ]
    if len(numeric_cols) < 20:
        raise RuntimeError(f"Too few numeric pan-transcriptome columns detected: {len(numeric_cols)}")

    expr = expression[[gene_col, *numeric_cols]].copy()
    expr[gene_col] = expr[gene_col].astype(str)
    values = expr[numeric_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    pav = (values >= 1.0).astype(np.int8)
    freq = pav.mean(axis=1)
    keep = (freq >= 0.05) & (freq <= 0.95)
    selected = (freq * (1 - freq)).where(keep, 0).sort_values(ascending=False).head(2000).index
    pav_sel = pav.loc[selected]
    gene_ids = expr.loc[selected, gene_col].astype(str).tolist()

    sample_by_gene = pav_sel.T
    sample_by_gene.columns = gene_ids
    sample_by_gene.insert(0, "sample_id", sample_by_gene.index)
    sample_by_gene.reset_index(drop=True).to_csv(out / "pangenome_gene_presence_absence.tsv", sep="\t", index=False)

    gene_by_sample = pav_sel.copy()
    gene_by_sample.insert(0, "gene_id", gene_ids)
    gene_by_sample.to_csv(out / "pangenome_gene_presence_absence.features_by_sample.tsv", sep="\t", index=False)

    write_text(
        out / "pav_answer_key.yaml",
        "\n".join([
            "dataset_id: public-maize-pantranscriptome-derived-epav-subset",
            "source: Zenodo 4994698 maize 503-genotype pan-transcriptome FPKM matrix",
            "derivation: expression presence/absence proxy; FPKM >= 1 encoded as 1",
            "caveat: derived ePAV, not direct genomic gene presence/absence from assemblies",
            f"n_samples: {len(numeric_cols)}",
            f"n_features: {len(gene_ids)}",
            "files:",
            f"  sv.vcf: {sha256_file(out / 'sv.vcf')}",
            f"  pangenome_gene_presence_absence.tsv: {sha256_file(out / 'pangenome_gene_presence_absence.tsv')}",
            f"  pangenome_gene_presence_absence.features_by_sample.tsv: {sha256_file(out / 'pangenome_gene_presence_absence.features_by_sample.tsv')}",
        ]) + "\n",
    )


def build_metabolite(rawdir: Path, outdir: Path) -> None:
    out = ensure_dir(outdir / "crop_multiomics")
    data = json.load((rawdir / "metabolomics_workbench" / "ST001947.mwtab.txt").open(encoding="utf-8"))
    metabolite_data = data.get("MS_METABOLITE_DATA") or {}
    raw = metabolite_data.get("Data") or metabolite_data.get("DATA") or metabolite_data.get("data")
    if raw is None:
        raise RuntimeError(f"Cannot find MS_METABOLITE_DATA Data key. Keys: {list(metabolite_data)}")

    df = pd.DataFrame(raw)
    id_cols = [
        col for col in df.columns
        if str(col).lower() in {"metabolite", "metabolite_name", "metabolite id", "metabolite_id", "compound", "compound_name"}
    ]
    metabolite_col = id_cols[0] if id_cols else df.columns[0]
    sample_cols = [
        col for col in df.columns
        if col != metabolite_col and pd.to_numeric(df[col], errors="coerce").notna().mean() >= 0.5
    ]
    if len(sample_cols) < 5:
        raise RuntimeError(f"Too few numeric metabolite sample columns detected: {len(sample_cols)}")

    matrix = df[[metabolite_col, *sample_cols]].copy()
    matrix[metabolite_col] = matrix[metabolite_col].astype(str).str.replace(r"\s+", "_", regex=True)
    values = matrix.set_index(metabolite_col)[sample_cols].apply(pd.to_numeric, errors="coerce").T
    values.index.name = "sample_id"
    values = values.reset_index()
    values = values[["sample_id", *list(values.columns[1:301])]]
    values.to_csv(out / "metabolite_matrix.tsv", sep="\t", index=False)

    sample_rows = []
    for item in data.get("SUBJECT_SAMPLE_FACTORS") or []:
        row = {"sample_id": item.get("Sample ID"), "subject_id": item.get("Subject ID")}
        row.update({str(k): v for k, v in (item.get("Factors") or {}).items()})
        extra = item.get("Additional sample data") or {}
        for key in ["Buckler.Gardner_Genotype", "Panzea_genotype_hmp321", "Panzea_phenotype_282NAM", "sampleFW.mg", "Note"]:
            if key in extra:
                row[key] = extra[key]
        sample_rows.append(row)
    pd.DataFrame(sample_rows).to_csv(out / "metabolite_sample_metadata.tsv", sep="\t", index=False)

    write_text(
        out / "metabolite_answer_key.yaml",
        "\n".join([
            "dataset_id: public-metabolomics-workbench-st001947-maize-metabolite-subset",
            "source: Metabolomics Workbench ST001947 / AN003169",
            "organism: Zea mays",
            "assay: GC-TOFMS shoot metabolite profiling",
            "subset_rule: all detected numeric samples, first 300 metabolite features",
            f"n_samples: {values.shape[0]}",
            f"n_metabolites: {values.shape[1] - 1}",
            "caveat: standalone metabolomics fixture; not guaranteed sample-aligned with CLIM4OMICS or pan-transcriptome genotype fixtures",
            "files:",
            f"  metabolite_matrix.tsv: {sha256_file(out / 'metabolite_matrix.tsv')}",
            f"  metabolite_sample_metadata.tsv: {sha256_file(out / 'metabolite_sample_metadata.tsv')}",
        ]) + "\n",
    )


def build_giab(rawdir: Path, outdir: Path) -> None:
    out = ensure_dir(outdir / "giab_hg002_wgs")
    source_vcf = rawdir / "giab_hg002" / "HG002_GRCh38_1_22_v4.2.1_benchmark.vcf.gz"
    target_vcf = out / "known_truth.vcf"
    with gzip.open(source_vcf, "rt", encoding="utf-8") as src, target_vcf.open("w", encoding="utf-8") as dst:
        records = 0
        for line in src:
            if line.startswith("#"):
                dst.write(line)
            elif line.split("\t", 1)[0] == "chr1" and records < 500:
                dst.write(line)
                records += 1
            elif records >= 500:
                break

    shutil.copyfile(
        rawdir / "giab_hg002" / "HG002_GRCh38_1_22_v4.2.1_benchmark_noinconsistent.bed",
        out / "high_confidence_regions.bed",
    )


def write_combined_checksums(outdir: Path) -> None:
    paths = []
    for name in ["g2f_maize_gxe", "maize_pangenome_sv", "crop_multiomics", "giab_hg002_wgs"]:
        paths.extend(sorted((outdir / name).glob("*")))
    lines = [
        f"{sha256_file(path)}  {path.relative_to(outdir).as_posix()}"
        for path in paths
        if path.is_file()
    ]
    write_text(outdir / "new_public_subset_SHA256SUMS.txt", "\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build extended public BioMind fixtures from cached raw downloads.")
    parser.add_argument("--rawdir", default=str(DEFAULT_RAWDIR), help="Raw public data cache directory.")
    parser.add_argument("--outdir", default=str(DEFAULT_OUTDIR), help="Fixture output directory.")
    args = parser.parse_args()

    rawdir = Path(args.rawdir).resolve()
    outdir = ensure_dir(Path(args.outdir).resolve())
    build_g2f_gxe(rawdir, outdir)
    build_sv_and_epav(rawdir, outdir)
    build_metabolite(rawdir, outdir)
    build_giab(rawdir, outdir)
    write_combined_checksums(outdir)
    print(f"Generated extended public fixtures under {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
