# BM-REMAIN-GWAS-008__strict

输入与任务修订：20261020-fullanalysis。此版本要求实际分析，历史输出与评分对应旧题，不适用于本版本。

```text
【Bioinformatics Agent Benchmark Test】
benchmark_id: BM-REMAIN-GWAS-008
input_revision: 20261020-fullanalysis
ambiguity_level: strict
data_status: ready_verified
task_name: Population-genetics GWAS complete analysis

路径（绝对路径）：
PROJECT=/public/home/liusiqi/03.毕设/benchmarks/02.测试26.9/04.重测序与变异分析/10.BM-REMAIN-GWAS-008__strict
DATA=/public/home/liusiqi/03.毕设/benchmarks/02.测试26.9/04.重测序与变异分析/10.BM-REMAIN-GWAS-008__strict/data
OUT=/public/home/liusiqi/03.毕设/benchmarks/02.测试26.9/04.重测序与变异分析/10.BM-REMAIN-GWAS-008__strict/output_inputfix_20261020
正式输入以 PROJECT/input_manifest.json 为准。data_inventory.tsv 与 input_status.tsv 是本版本有效清单；带旧日期后缀的清单只作为历史保存，不能补充本题输入。

执行要求：
Linux/Bash；按下文指定的绝对软件路径执行。输入只读，工作副本、索引、脚本、模型、表格、图片和日志全部写入 OUT；不写旧 output/ 或其他历史输出。不得读取历史结果、评判规则、隐藏答案或未列入清单的数据。无需安装 BioMind 或同名 skill；可使用指定原生模型与明示的等价实现。不得安装或修改共享软件环境。
只允许实际执行成功的阶段标记 completed；文件存在、软件安装或格式通过均不等于模型运行完成。遇到真实错误保存命令、退出码、stderr 和阶段原因，继续独立阶段。本版没有故意缺失的关键输入，也没有“仅交充分性审计即可”的成功条件。
全量大 VCF 分析通过 Slurm 申请资源；使用实际分配的额度。已核实 AMD_9A14 分区账户 zhangxingtan、QoS user_liusiqi，单核内存上限 3600 MB；例如 4 CPU/12 GB，记录实际作业 ID。队列变化时按实时错误记录，不将调度失败写成数据缺失。
固定随机种子 20260603。不同版本的性能、结果和评分不可直接混用。

目标：以 215 个样本的 C16_0 性状完成 EMMAX MLM GWAS、绘图、显著位点、候选区及真实 LD 分析。
正式输入位于 DATA/gwas/：Real_snp.vcf.gz、phenotype.tsv、Real.gff3、Real_snp.regenerated.3.Q、Q_row_map.json、Q_sample_map.tsv、Q_with_sample_ids.tsv、formal_samples.tfam、covariates.FID_IID_intercept_Q1_Q2.tsv、vcf_to_plink_chromosome.tsv。原 Real_trait.C16_0.table 是无表头两列文件，phenotype.tsv 为其无数值改动的带表头版本，列 sample_id、C16_0；不得从不存在的原表头猜测性状。单位原教学数据未说明，报告保留原尺度，不擅自赋予单位。
软件：/public/home/liusiqi/03.毕设/benchmarks/biomind_autotest_20261011_preflight/dependency_plan/proposed-envs/bio_gwas_cli/bin/plink、emmax-kin-intel64、emmax-intel64；绘图 Rscript 为 /public/home/liusiqi/anaconda3/envs/rna_env/bin/Rscript。表格处理用 /public/home/liusiqi/anaconda3/envs/biomind/bin/python。EMMAX 不可用时不能替换成 TASSEL、普通回归或其他模型。
1. 检查 VCF 完整性、二倍体 GT、样本唯一性、变异数、缺失率、染色体与 GFF 坐标标签。保留输入校验和。用 PLINK --vcf 指定文件 --double-id --maf 0.05 --geno 0.1 --recode 12 transpose --output-missing-genotype 0 --set-missing-var-ids @:# --allow-extra-chr --keep-allele-order，输出 OUT/prep/markers。额外染色体名按 vcf_to_plink_chromosome.tsv 在工作 TPED 副本中转换为整数；保留原 CHR/POS/marker 对照，注释与作图时恢复。检测 marker ID 重复，必要时给冲突记录加 REF/ALT 以确保唯一并保存映射。
2. 严格按 TFAM 的 FID/IID 显式连接 phenotype 和新版 Q 的 sample_id，不以长度相同代替身份。Q_row_map.json 绑定新版 Q 与生成时 FAM 的校验和；核对后按 ID 重排。新 Q 是 ADMIXTURE K=3、seed=20261012 的重建输入，不声称恢复旧无身份 Q。共变量使用 FID IID intercept Q1 Q2 五列，无表头；Q3 与截距冗余，保留在原始 Q 但不加入模型。检查共变量秩为 3。
3. 对实际过滤后的 TPED 用 emmax-kin-intel64 -v -d 10 计算 K。运行可生成 .aBN.kinf，必须按实际生成文件名取 K，不能假定 .aIBS.kinf 或使用日志。EMMAX -v -d 10 -t TPED前缀 -p 三列表型 -k 实际K -c 五列共变量 -o OUT/gwas/emmax。表型为 FID IID C16_0，无表头，保持 TFAM 顺序。
4. 本机 EMMAX .ps 实际四列为 marker、beta、SE、p；按 marker 联接 TPED，不能误用 SE 作 p。检查有效 p∈(0,1]，记录无效记录原因，M 为实际有效检验数，阈值 0.05/M。绘制所有有效 p 的 Manhattan/QQ，导出 gwas/gwas_results.tsv、gwas/manhattan_input.tsv、plots/manhattan.png、plots/qqplot.png、significant_sites.tsv。
5. 显著位点同染色体 ±50 kb 合并为候选区，与 Real.gff3 基因区间相交，输出 ldblock_candidate_regions.tsv、candidate_genes.tsv。使用同一批保留样本、真实基因型在候选区计算成对 r²，明确其为局部 LD 结果；±50 kb 窗口本身不等于统计 LD block。无显著位点时写带表头的空候选表和 no-hit 说明，属于有效结果，不降低阈值凑结果。原 GFF 未提供可验证公共组装 accession，不外推到其他棉花参考版本。
成功条件：全量正式 VCF 完成 QC、K、EMMAX、图和候选/LD 阶段；小规模验收不能代替本题全量分析。

交付共通要求：
在 OUT 写 report.md、reproduce.sh、stage_status.tsv、commands.json（包含软件版本、命令、退出码）。报告列明输入版本、实际使用/排除的样本和特征、参数、方法限制、运行状态；所有结果必须来自本次运行。reproduce.sh 使用绝对入口，重复执行不得破坏输入与历史结果。
```
