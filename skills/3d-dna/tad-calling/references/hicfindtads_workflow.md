# hicFindTADs Workflow Reference

Use this reference when a user asks for a full TAD calling workflow, has many samples, or needs a reproducible command plan.

## Sample Input Strategy

For 1-3 samples, repeated `--sample` is readable:

```bash
python "${SKILL_DIR}/scripts/call_tads.py" \
  --sample sampleA /data/sampleA.mcool \
  --sample sampleB /data/sampleB.mcool \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer
```

For more samples, require a sample sheet:

```tsv
sample	matrix
sampleA	/data/sampleA.mcool
sampleB	/data/sampleB.mcool
sampleC	/data/sampleC.mcool
```

Run:

```bash
python "${SKILL_DIR}/scripts/call_tads.py" \
  --sample-sheet samples.tsv \
  --resolution-bp 40000 \
  --out-root TADs/HiCExplorer
```

## Resolution Policy

Do not infer a resolution for `.mcool` files. Ask the user to choose one after inspecting available resolutions.

```bash
python -c "import cooler; print(cooler.fileops.list_coolers('/data/sampleA.mcool'))"
```

Accepted explicit resolution forms:

- Global command argument: `--resolution-bp 40000`
- Full matrix URI: `/data/sampleA.mcool::/resolutions/40000`
- Sample sheet column: `resolution_bp`

Use one resolution per multi-sample comparison unless the user explicitly wants separate runs.

## Native hicFindTADs Command Shape

The wrapper expands each sample to:

```bash
hicFindTADs \
  --matrix /data/sampleA.mcool::/resolutions/40000 \
  --outPrefix TADs/HiCExplorer/40000/sampleA \
  --correctForMultipleTesting fdr
```

Native outputs:

- `sampleA_boundaries.bed`
- `sampleA_boundaries.gff`
- `sampleA_domains.bed`
- `sampleA_score.bedgraph`
- `sampleA_tad_score.bm`

## Reporting

Use `tad_domain_stats.tsv` for sample-level reporting:

- TAD count
- maximum/minimum/mean/median domain length
- total covered length
- boundary count

Use `tad_domains_long.tsv` and `tad_boundaries_long.tsv` for downstream overlap, visualization, or annotation.
