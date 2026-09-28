#!/usr/bin/env python3
"""Append strand-aware plant cytosine context to modkit bedMethyl."""

from __future__ import annotations

import argparse
import gzip
from pathlib import Path
from typing import Dict, Iterable, TextIO, Tuple


FaiEntry = Tuple[int, int, int, int]


def opener(path: str, mode: str):
    return gzip.open(path, mode) if path.endswith(".gz") else open(path, mode)


def read_fai(fasta: Path) -> Dict[str, FaiEntry]:
    fai = Path(str(fasta) + ".fai")
    if not fai.exists():
        raise SystemExit(f"Missing FASTA index: {fai}. Run: samtools faidx {fasta}")
    entries: Dict[str, FaiEntry] = {}
    with fai.open() as handle:
        for line in handle:
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 5:
                continue
            name = fields[0]
            entries[name] = tuple(map(int, fields[1:5]))  # type: ignore[assignment]
    return entries


class IndexedFasta:
    def __init__(self, fasta: Path) -> None:
        self.fasta = fasta
        self.index = read_fai(fasta)
        self.handle = fasta.open("rb")

    def fetch(self, chrom: str, start: int, end: int) -> str:
        if chrom not in self.index:
            return ""
        length, offset, line_bases, line_width = self.index[chrom]
        start = max(0, start)
        end = min(length, end)
        if end <= start:
            return ""
        chunks = []
        pos = start
        while pos < end:
            line_idx = pos // line_bases
            in_line = pos % line_bases
            take = min(end - pos, line_bases - in_line)
            byte_offset = offset + line_idx * line_width + in_line
            self.handle.seek(byte_offset)
            chunks.append(self.handle.read(take).decode("ascii"))
            pos += take
        return "".join(chunks).upper()

    def close(self) -> None:
        self.handle.close()


def context_for(fasta: IndexedFasta, chrom: str, start: int, strand: str) -> str:
    if strand == "+":
        seq = fasta.fetch(chrom, start, start + 3)
        if len(seq) < 3 or seq[0] != "C":
            return "UNKNOWN"
        if seq[1] == "G":
            return "CG"
        if seq[2] == "G":
            return "CHG"
        return "CHH"
    if strand == "-":
        seq = fasta.fetch(chrom, start - 2, start + 1)
        if len(seq) < 3 or seq[2] != "G":
            return "UNKNOWN"
        if seq[1] == "C":
            return "CG"
        if seq[0] == "C":
            return "CHG"
        return "CHH"
    return "UNKNOWN"


def annotate_rows(in_handle: TextIO, out_handle: TextIO, fasta: IndexedFasta) -> Dict[str, int]:
    counts = {"CG": 0, "CHG": 0, "CHH": 0, "UNKNOWN": 0}
    wrote_header = False
    for line in in_handle:
        line = line.rstrip("\n")
        if not line:
            continue
        if line.startswith("#"):
            out_handle.write(line + "\n")
            continue
        fields = line.split("\t")
        if len(fields) < 6:
            out_handle.write(line + "\tUNKNOWN\n")
            counts["UNKNOWN"] += 1
            continue
        chrom, start_text, strand = fields[0], fields[1], fields[5]
        try:
            start = int(start_text)
        except ValueError:
            if not wrote_header:
                out_handle.write(line + "\tmethylation_context\n")
                wrote_header = True
                continue
            context = "UNKNOWN"
        else:
            context = context_for(fasta, chrom, start, strand)
        counts[context] += 1
        out_handle.write(line + "\t" + context + "\n")
    return counts


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Append CG/CHG/CHH/UNKNOWN context to a modkit bedMethyl file."
    )
    parser.add_argument("--bedmethyl", required=True, help="Input bedMethyl, optionally .gz")
    parser.add_argument("--reference", required=True, help="Reference FASTA with .fai index")
    parser.add_argument("--output", required=True, help="Output TSV, optionally .gz")
    args = parser.parse_args(argv)

    fasta = IndexedFasta(Path(args.reference))
    try:
        with opener(args.bedmethyl, "rt") as in_handle, opener(args.output, "wt") as out_handle:
            counts = annotate_rows(in_handle, out_handle, fasta)
    finally:
        fasta.close()
    print("\t".join(["context", "count"]))
    for key in ("CG", "CHG", "CHH", "UNKNOWN"):
        print(f"{key}\t{counts[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
