---
name: snpeff-annotation
description: Annotate VCF SNPs and indels with snpEff using an existing database or a custom FASTA and GTF/GFF3/GenBank
  reference. Use for database building, variant effects and ANN summaries; variant calling must already be complete.
compatibility: 需要本技能 references/dependencies.md 列明的现有软件；不自动安装。输入输出位于分析项目。
---


## 资源定位与依赖

使用当前 Agent 的标准技能发现与文件读取能力加载本文件。`scripts/`、`references/`、
`assets/` 相对本 SKILL.md 的实际目录；输入、输出和样本清单相对本次分析项目。
命令中的 `${SKILL_DIR}` 必须先设为实际技能目录的绝对路径；该变量不由宿主自动提供。
例如，从已加载位置解析目录后，以带引号的脚本绝对路径执行，保持工作目录为分析项目，
不要在技能安装目录写结果。跨技能资源按其实际目录定位；分享完整技能包时保留目录结构。
软件和参考数据库来自已有环境或用户显式提供的位置。依赖见
[dependencies.md](references/dependencies.md)；安装、升级或修改环境需要事先明确授权。
本文命令可由任意提供命令执行能力的 Agent 或终端使用；不会在读取技能时安装或体检环境。

# snpEff Annotation

Annotate an existing VCF, choosing a matching pre-built database or building a
custom one. Organize the steps around the actual inputs and requested outputs.

## Reference and runtime

- Match the exact assembly, annotation release and contig names across VCF,
  FASTA and GTF/GFF3. Species names alone do not establish compatibility.
  Parse VCF contig IDs separately from comma-delimited length/assembly metadata.
- `genome-version` is a database key: it must agree with the config entry and
  database directory. Choose it from the actual reference, then record and
  reuse it. Do not infer an official key or codon table from taxonomy alone.
- Reuse the verified `bio_gatk` environment and supplied Java/JAR paths.
  The declared healthcheck resolves the installed JAR and checks Java.
  If it fails, use the declared runtime installer when permitted; report a
  blocked dependency if it cannot be resolved. Avoid whole-filesystem searches
  and guessed Conda prefixes.
- Keep database name, config, data directory and output paths explicit in the
  plan. If steps are split, publish prepared files or a completed database as
  outputs and refer to them as downstream inputs. Shell-local variables do
  not persist across steps.

## Existing database

1. Verify that the database matches the VCF assembly and annotation release.
   List or download a database only when network access is allowed and its
   identity is established: `java -jar snpEff.jar databases` or
   `java -jar snpEff.jar download -v <genome-version>`.
2. Check that the selected database can be loaded using the intended Java/JAR,
   config and data directory. A failed load check prevents annotation; inspect
   its cause before deciding whether rebuilding is needed.
3. Annotate using the verified concrete paths. Command shape:

   ```bash
   java -Xmx8g -jar snpEff.jar ann -c snpEff.config -dataDir data -v genome_version input.vcf > annotated.vcf
   ```

   Replace example names with the recorded values. Preserve logs and the
   actual snpEff exit code. Check the output as described below.

## Custom database

Read [config templates](references/snpeff_config_templates.md) for layouts and
codon-table configuration, and [snpEff notes](references/snpeff_notes.md) for
format-specific details.

1. Use a matching reference FASTA and GTF/GFF3, or an authoritative GenBank
   source. Add `<genome-version>.genome : <name>` to a task-local config.
2. Stage the files under the chosen data directory:
   - FASTA: `genomes/<genome-version>.fa` or `<genome-version>/sequences.fa`.
   - Annotation: `<genome-version>/genes.gtf`, `genes.gff` or `genes.gbk`.
3. Build with the corresponding `-gtf22`, `-gff3` or `-genbank` flag. Put the
   subcommand immediately after the JAR, before its options:

   ```bash
   java -Xmx8g -jar snpEff.jar build -c snpEff.config -dataDir data -gtf22 -v genome_version
   ```

4. Supply CDS/protein validation files when available. For missing optional
   files, disable only the unavailable check with `-noCheckCds` and/or
   `-noCheckProtein`; record the skipped checks and limitations in the report.
   Do not silently skip a validation explicitly required by the task.
5. Require a successful inner build exit, a nonempty
   `data/<genome-version>/snpEffectPredictor.bin`, no fatal build result such as
   `Database check failed`, and a successful database-load preflight before
   annotation. Config files, copied inputs or a wrapper exit of zero alone
   do not establish build success.
6. Preserve build logs, CDS/START/STOP warnings and Total Errors statistics.
   Quality warnings are distinct from process failure. Do not sum overlapping
   error categories or describe their complement as accuracy. Apply only the
   task's approved quality thresholds. Then annotate using the same database.

## Results and validation

- Verify the VCF header contains `##SnpEffVersion` and `##INFO=<ID=ANN`;
  inspect ANN records and chromosome-not-found warnings. Unexpectedly many
  intergenic calls can indicate a reference mismatch. Do not blindly add or
  remove `chr` prefixes.
- A variant can have multiple ANN entries. Report variant counts separately
  from transcript/allele annotation counts, effects, impact and warnings.
- For `snpeff-ann-marginals-v1`, use three TSV sections separated by blank lines:
  `metric/value`, `annotation_impact/count`, `sequence_ontology_effect/count`.
  Metrics are `variants_total`, `variants_with_ann`, `ann_records_total`,
  `header_has_snpeff_version` and `header_has_ann_info`. Counts are nonnegative
  integers; flags are true/false. Count variants as VCF records, not ALT alleles.
  Each marginal sums to ANN entries; keep a compound SO effect as one category.
  Reject missing sections, duplicate keys and inconsistent counts. A valid
  table does not independently validate the original VCF or biological quality.
- Optional helpers: `scripts/ann_summary.py` summarizes/validates this TSV;
  `scripts/snpeff_checks.py` separates build process, predictor, load and
  quality facts. Use their supplied resource paths and inspect their CLI
  before invocation. Honor a different summary format if the plan specifies it.
- Write the requested report, reproduction script and stage status from
  verified results. Preserve the actual successful code, argv, working
  directory, config and nonsecret dependencies; identify whether reproduction
  requires the existing workspace. Do not claim an independent replay unless
  it ran. Report completion only to the extent supported by the actual tool receipts.
- Produce summary HTML when requested. Compress/index VCF only when requested
  and the needed bgzip/tabix tools are available.

## Legacy and references

The renderer metadata is retained for existing legacy sessions. For an
explicit `legacy_renderer_v1` task, use the declared renderer and its CLI help;
normal execution follows the command patterns above.

- [snpEff notes](references/snpeff_notes.md): database choice, formats and failures.
- [Config templates](references/snpeff_config_templates.md): custom config and layout.
- Official documentation: [running](https://pcingola.github.io/SnpEff/snpeff/running/),
  [building](https://pcingola.github.io/SnpEff/snpeff/build_db/),
  [ANN format](https://pcingola.github.io/SnpEff/snpeff/inputoutput/).
