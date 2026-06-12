#!/usr/bin/env python3
import argparse
import gzip
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description="Convert simple VCF SNP genotypes into per-sample FASTA alignment.")
    p.add_argument("--vcf", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--reference", help="Accepted for workflow compatibility; SNP REF alleles are read from VCF.")
    args = p.parse_args()
    opener = gzip.open if str(args.vcf).endswith(".gz") else open
    samples = []
    seqs = {}
    with opener(args.vcf, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("##"):
                continue
            if line.startswith("#CHROM"):
                samples = line.rstrip().split("\t")[9:]
                seqs = {s: [] for s in samples}
                continue
            parts = line.rstrip().split("\t")
            ref, alts = parts[3], parts[4].split(",")
            if len(ref) != 1 or any(len(a) != 1 for a in alts):
                continue
            amap = {"0": ref, **{str(i): a for i, a in enumerate(alts, 1)}}
            for s, gt in zip(samples, parts[9:]):
                alleles = gt.split(":")[0].replace("|", "/").split("/")
                base = amap.get(alleles[0], "N") if alleles and alleles[0] != "." else "N"
                seqs[s].append(base)
    out = Path(args.output); out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for s in samples:
            fh.write(f">{s}\n{''.join(seqs[s])}\n")
    print(f"wrote alignment -> {out}")


if __name__ == "__main__":
    main()
