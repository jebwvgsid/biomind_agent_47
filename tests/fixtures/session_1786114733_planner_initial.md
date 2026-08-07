# Section 1: Requirement Analysis

## 输入文件
- **VCF**: `benchmarks/01.测试/04.重测序/Part2.vairant_annotation/data/all.filtered.snp.vcf`
- **参考基因组**: `benchmarks/01.测试/04.重测序/Part2.vairant_annotation/data/genome.fasta`
- **GTF注释**: `benchmarks/01.测试/04.重测序/Part2.vairant_annotation/data/genome.gtf`

## 工具需求
- **snpEff** (via `genomics/snpeff-annotation` skill)
- **Java** (Temurin-21, 已通过 healthcheck)
- **Conda环境**: `bio_gatk`

## 运行时状态
- **环境**: `bio_gatk` — 已通过 healthcheck，状态 `ready`
- **snpEff JAR**: `/public/home/liusiqi/anaconda3/envs/bio_gatk/share/snpeff-5.3.0a-0/snpEff.jar`
- **Java**: `/public/home/liusiqi/03.毕设/source_tools/temurin-21`
- **无需安装**

## 关键约束
- 必须使用 `-gtf22` 构建本地 snpEff 数据库
- 禁止猜测物种数据库
- 禁止忽略染色体命名不匹配
- 输出目录: `benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-ANNOT-007/strict`

## 预期产物
1. `annotated.vcf`
2. `snpEff_summary.html`
3. `ann_effect_summary.tsv`
4. `ann_gene_summary.tsv`
5. `report.md`
6. `reproduce.sh`

## 资源估算
- **CPU**: 2-4核
- **RAM**: 8-16 GB (数据库构建 + 注释)
- **磁盘**: ~1-2 GB (数据库 + 输出文件)
- **时间**: 10-30分钟 (取决于基因组大小)

---

# Section 2: Step-by-Step Execution Plan

## Step 1: 验证输入文件完整性
- **Tool**: bash (文件检查)
- **Execution guidance**: 使用 `ls -lh` 和 `test -s` 检查三个输入文件是否存在且非空。检查 VCF 是否为有效格式（`file` 命令），检查 FASTA 索引文件 `.fai` 是否存在，若不存在则需创建。
- **Input**:
  - `benchmarks/01.测试/04.重测序/Part2.vairant_annotation/data/all.filtered.snp.vcf`
  - `benchmarks/01.测试/04.重测序/Part2.vairant_annotation/data/genome.fasta`
  - `benchmarks/01.测试/04.重测序/Part2.vairant_annotation/data/genome.gtf`
- **Output**: 验证报告（文件存在性、大小、格式）
- **Success criteria**: 所有三个文件存在且非空；FASTA 有索引或成功创建索引
- **Cautions**: 检查染色体命名一致性（VCF、FASTA、GTF 中的染色体名必须匹配）；如果 FASTA 无 `.fai` 索引，snpEff 构建数据库会失败
- **Risk**: LOW — 文件路径已明确
- **Assumptions**: 文件路径相对于项目根目录解析；当前工作目录可能不是项目根目录
- **Required constraints**: 必须使用用户指定的输入文件，不得替换
- **Input contract**: VCF (v4.1+), FASTA (indexed), GTF (ENSEMBL/GFF3-like)
- **Output contract**: 验证通过或 blocked
- **Environment contract**: bash（无需特定环境）
- **Depends on**: 无
- **Allowed fallbacks**: 若 `.fai` 缺失，允许使用 `samtools faidx` 创建
- **Forbidden fallbacks**: 不得使用 tests/demo_ref.fa 或 demo.vcf；不得忽略染色体命名不匹配

## Step 2: 构建 snpEff 自定义数据库并注释 VCF
- **Tool**: `genomics/snpeff-annotation` skill (renderer: `genomics.snpeff_annotation`)
- **Execution guidance**: 调用 renderer 入口脚本 `render_snpeff_annotation_scripts.py`，传入以下参数：
  - `vcf`: 绝对路径到 `all.filtered.snp.vcf`
  - `genome_version`: 自定义基因组名称（如 `custom_genome`）
  - `annotation_gtf`: 绝对路径到 `genome.gtf`（通过 `-gtf22` 路由）
  - `reference_fasta`: 绝对路径到 `genome.fasta`
  - `outdir`: 输出目录绝对路径
  - `output_vcf`: `annotated.vcf`
  - `snpeff_jar`: `/public/home/liusiqi/anaconda3/envs/bio_gatk/share/snpeff-5.3.0a-0/snpEff.jar`
  - `data_dir`: snpEff 数据目录（默认 `snpEff/data`）
  - `allow_unchecked_build`: `false`（严格模式，不得跳过验证）
  - 其他参数使用 renderer 默认值
- **Input**: 所有三个输入文件 + snpEff JAR + Java
- **Output**:
  - `annotated.vcf`
  - `snpEff_summary.html`
  - `ann_effect_summary.tsv`
  - `ann_gene_summary.tsv`
  - `report.md`
  - `reproduce.sh`
- **Success criteria**: 所有输出文件生成且非空；`annotated.vcf` 包含 ANN 字段；`snpEff_summary.html` 可打开；`ann_effect_summary.tsv` 包含效应统计
- **Cautions**:
  - 染色体命名必须在 VCF/FASTA/GTF 中一致，否则 snpEff 会报错或跳过所有变异
  - 数据库构建可能因内存不足失败，需确保足够 RAM
  - 若 `data_dir` 中已有同名数据库，可能冲突，需清理或使用不同 `genome_version`
  - `-gtf22` 要求 GTF 格式严格符合规范
- **Risk**: MEDIUM — 数据库构建可能因染色体命名、GTF 格式、内存等问题失败
- **Assumptions**: GTF 文件格式正确；染色体命名一致；snpEff 数据目录可写
- **Required constraints**: 必须使用 `-gtf22`；不得猜测物种数据库；不得忽略染色体命名不匹配；数据库构建失败时 blocked
- **Input contract**: VCF (v4.1+), FASTA (indexed), GTF (valid GTF22)
- **Output contract**: 所有 6 个产物文件存在、非空、格式正确
- **Environment contract**: `bio_gatk` conda 环境；Java Temurin-21；snpEff 5.3.0a
- **Depends on**: Step 1（输入验证通过）
- **Allowed fallbacks**: 若默认 `data_dir` 不可写，允许使用输出目录下的子目录
- **Forbidden fallbacks**: 禁止使用预构建公共数据库；禁止跳过数据库验证；禁止输出假 ANN；禁止替换输入文件

---

# Execution Checklist

- [ ] Step 1: 验证输入文件完整性 | Goal: 确认所有输入文件存在、非空、格式正确 | Action: EXECUTE | Purpose: validation | Skills: [genomics/snpeff-annotation] | Stage: none | Batch: batch_1 | Done: 三个文件存在且非空；FASTA 已索引；染色体命名一致
- [ ] Step 2: 构建 snpEff 自定义数据库并注释 VCF | Goal: 使用 -gtf22 构建数据库并生成所有注释产物 | Action: EXECUTE | Purpose: analysis | Skills: [genomics/snpeff-annotation] | Stage: none | Batch: batch_1 | Done: 所有 6 个输出文件生成且非空；annotated.vcf 含 ANN 字段

---

# Section 4: Validation Checklist

## 最终输出验证
1. **annotated.vcf**
   - 文件存在且非空
   - 包含 `##snpEff` 头部行
   - INFO 字段包含 `ANN=` 注释
   - 变异数量与输入 VCF 一致（无丢失）

2. **snpEff_summary.html**
   - 文件存在且非空
   - 包含效应分布统计表
   - 可在浏览器中正常渲染

3. **ann_effect_summary.tsv**
   - 文件存在且非空
   - 包含效应类型和计数列
   - 总计与输入变异数一致

4. **ann_gene_summary.tsv**
   - 文件存在且非空
   - 包含基因名和变异计数

5. **report.md**
   - 文件存在且非空
   - 包含分析摘要、参数、统计信息

6. **reproduce.sh**
   - 文件存在且可执行
   - 包含完整可复现命令

## QC 指标
- **注释率**: ANN 字段非空的比例（通常 >80%）
- **效应分布**: 检查 HIGH/MODERATE/LOW/MODIFIER 分布是否合理
- **染色体匹配**: 确认所有染色体名在 VCF/FASTA/GTF 中一致
- **数据库构建日志**: 检查 `snpEff build` 输出，确认无错误

## 常见失败模式
1. **染色体命名不匹配**: VCF 使用 `chr1`，FASTA 使用 `1` → snpEff 无法匹配，注释率为 0
2. **GTF 格式错误**: 缺少 `gene_id` 或 `transcript_id` 属性 → 数据库构建失败
3. **内存不足**: 大基因组构建数据库时 Java heap space 溢出 → 增加 `-Xmx` 参数
4. **snpEff 数据目录权限**: 默认 `data/` 目录不可写 → 使用自定义 `-dataDir`
5. **Java 版本不兼容**: 过旧的 Java 版本 → 使用 Temurin-21
