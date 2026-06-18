# Database Decision

Functional enrichment is first a species, ID, database, and background decision problem; plotting comes after those checks pass.

## Model Plants

Arabidopsis:

- Prefer TAIR, `org.At.tair.db`, Ensembl Plants, or a project-specific Araport/TAIR annotation.
- Use `enrichGO` or custom GO `TERM2GENE` only after ID type is confirmed.
- Use KEGG Arabidopsis organism code only when IDs can be mapped to the supported KEGG key type.

Rice:

- Prefer RAPDB, MSU, Gramene, Ensembl Plants, or `org.Osativa.eg.db`.
- Convert RAPDB/MSU/LOC IDs explicitly before enrichment.
- Report the ID conversion table and conversion rate.

Maize, wheat, barley, tomato, soybean, cotton, and other crop species:

- Prefer Ensembl Plants, Gramene, Phytozome, pan-genome project annotation, or species-specific curated annotation.
- If no supported OrgDb exists, build custom `TERM2GENE`.

## Non-Model Angiosperms

Use this order:

1. Native genome annotation with GO, KEGG, KO, InterPro, Pfam, MapMan, or product terms.
2. eggNOG-mapper annotation.
3. InterProScan annotation.
4. KO annotation from BlastKOALA, KofamKOALA, KAAS, or DIAMOND+KEGG mapping.
5. Orthology to a close species.
6. Broad plant database only as low-confidence inference.

## Gymnosperms And Distant Non-Models

Do not default to Arabidopsis or rice.

Use:

1. Native annotation if available.
2. Close gymnosperm or lineage-specific reference if available.
3. eggNOG, InterPro, Pfam, or KO annotation.
4. Orthology-based inference with clear warning.
5. Viridiplantae/eukaryote-level annotation only as a last resort.

## Animals, Fungi, Bacteria, And Other Taxa

- Prefer native species OrgDb, Ensembl, Reactome, KEGG, UniProt, or project annotation.
- Use orthology only when native support is missing, and label it.
- Do not reuse plant resources for non-plant species.

## KEGG And KO

If the target species has a KEGG organism code:

- Use native KEGG enrichment only when input IDs match a supported key type or can be mapped reliably.

If the target species has no KEGG organism code:

- Use KO/K-number enrichment with custom `TERM2GENE`.
- Do not force another species code.

## Reactome And Plant Reactome

- Use Reactome for supported animal/model species when IDs can be mapped.
- Use Plant Reactome or curated pathway files for plants when available.
- For unsupported species, use orthology-mapped pathways only with explicit caveats.

## Custom TERM2GENE

Use custom `TERM2GENE` when:

- Species is non-model.
- Database does not support the input IDs.
- Annotation comes from eggNOG, InterProScan, Pfam, KO, MapMan, OrthoFinder, or a project database.
- The user provides gene-to-term mappings.

Minimum `TERM2GENE` columns:

```text
term_id    gene_id
```

Optional `TERM2NAME` columns:

```text
term_id    term_name
```

Always report how `TERM2GENE` was built, which annotation version was used, and how many genes and terms remain after filtering.
