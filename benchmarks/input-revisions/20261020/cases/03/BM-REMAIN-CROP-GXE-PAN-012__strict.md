# BM-REMAIN-CROP-GXE-PAN-012__strict

输入与任务修订：20261020-fullanalysis。此版本要求实际分析，历史输出与评分对应旧题，不适用于本版本。

```text
【Bioinformatics Agent Benchmark Test】
benchmark_id: BM-REMAIN-CROP-GXE-PAN-012
input_revision: 20261020-fullanalysis
ambiguity_level: strict
data_status: ready_verified
data_status: ready_verified
task_name: GxE, expression-presence and SV phenotype prediction

路径（绝对路径）：
PROJECT=/public/home/liusiqi/03.毕设/benchmarks/02.测试26.9/05.作物育种与多组学/10.BM-REMAIN-CROP-GXE-PAN-012__strict
DATA=/public/home/liusiqi/03.毕设/benchmarks/02.测试26.9/05.作物育种与多组学/10.BM-REMAIN-CROP-GXE-PAN-012__strict/data
OUT=/public/home/liusiqi/03.毕设/benchmarks/02.测试26.9/05.作物育种与多组学/10.BM-REMAIN-CROP-GXE-PAN-012__strict/output_inputfix_20261020
正式输入以 PROJECT/input_manifest.json 为准。data_inventory.tsv 与 input_status.tsv 是本版本有效清单；带旧日期后缀的清单只作为历史保存，不能补充本题输入。

执行要求：
Linux/Bash；按下文指定的绝对软件路径执行。输入只读，工作副本、索引、脚本、模型、表格、图片和日志全部写入 OUT；不写旧 output/ 或其他历史输出。不得读取历史结果、评判规则、隐藏答案或未列入清单的数据。无需安装 BioMind 或同名 skill；可使用指定原生模型与明示的等价实现。不得安装或修改共享软件环境。
只允许实际执行成功的阶段标记 completed；文件存在、软件安装或格式通过均不等于模型运行完成。遇到真实错误保存命令、退出码、stderr 和阶段原因，继续独立阶段。本版没有故意缺失的关键输入，也没有“仅交充分性审计即可”的成功条件。
全量大 VCF 分析通过 Slurm 申请资源；使用实际分配的额度。已核实 AMD_9A14 分区账户 zhangxingtan、QoS user_liusiqi，单核内存上限 3600 MB；例如 4 CPU/12 GB，记录实际作业 ID。队列变化时按实时错误记录，不将调度失败写成数据缺失。
固定随机种子 20260603。不同版本的性能、结果和评分不可直接混用。

目标：完成 GxE 环境反应规范和 rrBLUP 预测，并独立完成表达存在特征、结构变异的表型预测。所有分支都要求真实拟合。
软件：/public/home/liusiqi/03.毕设/benchmarks/biomind_autotest_20261011_preflight/dependency_plan/proposed-envs/bio_crop_gs_r43/bin/Rscript、rrBLUP；CERIS-JGRA 原函数 /public/home/liusiqi/03.毕设/benchmarks/runtime_setup_20261014/output/sources/CERIS_JGRA/Sub_functions.r。可调用原生函数或按下述公式写可追踪的等价 R 实现，不得用 sklearn 替代。上游驱动含自动安装语句，不执行其安装段。

A. GxE（DATA/gxe/）
正式输入：phenotype_multi_environment.tsv、genotype_matrix.tsv、observation_identity.tsv、source_plot_records.csv、source_weather_records.csv、weather_source_map.tsv、weather_daily.tsv、window_coverage.tsv、analysis_window.tsv。旧 environment_timeseries.tsv、growth_stage_window.tsv 是来源副本，不与新天气叠加。
1. 895 行表型为小区观测，trait_yield 单位 bu/A；用源行号、Plot_ID 逐条核对。同 sample_id×environment_id 的不同小区不是重复导入。在拟合层按 sample_id×environment_id 求等权均值，保存 n_plot、均值、标准差和原行映射；保留原始观测。block 缺失 257 行维持缺失，不猜区组。
2. 31×500 的基因型是已给定数值特征，含分数/缺值，不宣称重新核实了二倍体整数剂量。按 sample_id 显式连接。每个训练折删除全缺失列，余列以训练均值插补、训练尺度中心化/标准化；常量列可删除或保持零列并记录。全缺失列的 NaN 均值不得送入 rrBLUP。
3. 天气是基因型×环境分析的环境协变量。固定日历 DOY 150..230、Temperature_C，不将此窗口称为所有材料真实开花灌浆期或播后天数。按官方映射连接 27 个环境；同源天气标签同日的非缺失温度观测先求等权日均，再对窗口内有效日求均值。不插补缺日，不把小时记录当重复日。
4. 预设有效天数/81 >=0.8 才拟合；从原始记录核验 weather_daily.tsv、window_coverage.tsv。25 个环境合格；IAH3/WIH1 不足者保留在排除表。阈值不按表型或模型表现调整。只有一个预设窗口，无监督选窗自由度；训练环境指数与表型均值的相关只作描述，不引入 DAP100 或另选窗口。
5. 对每 genotype 拟合 R lm(trait_yield ~ environmental_index)，至少 3 个环境且指数有变化；输出 intercept、slope、SE、环境数、R²。按 source_group 整组留出验证，共享天气源的所有环境同时移出训练折；每折重新拟合并预测留出环境，单列预测覆盖和外推。这检验已见材料的新环境预测。
6. 另将具备反应规范的 sample_id 排序，set.seed(20260603)，以 sample(rep(1:5,length.out=n)) 分配五折，整 genotype 留出。只用训练 genotype 的截距/斜率分别调用 rrBLUP::mixed.solve，预测留出材料两参数，再以 intercept_hat+slope_hat*target_index 重建表型。固定效应加法使用 as.numeric(beta)+as.vector(Ztest %*% u)，避免矩阵维度误加。训练折插补、中心化、特征保留清单必须可追溯。这检验新材料在已测天气环境下的预测，不等同上一种目标。
产物：gxe/field_check.tsv、plot_aggregation.tsv、weather_coverage.tsv、environmental_index_summary.tsv、reaction_norm_parameters.tsv、environment_group_oof.tsv、genotype_oof.tsv、genotype_oof_phenotypes.tsv、fold_audit.tsv、metrics.tsv、reaction_norm.png；不足以拟合的记录写原因及覆盖数，不虚构预测。

B. 表达存在特征预测（DATA/pangenome/epav_*）
输入 epav_matrix.tsv、epav_features.tsv、epav_sample_map.tsv、epav_phenotype.tsv、epav_cv_folds.tsv、epav_excluded_samples.tsv、provenance.json。
旧 PAV 因原始表每行尾部多余制表符造成基因/样本列错位。本版从校验通过的 Hirsch2014 FPKM 重新解析：全部 2000 个旧技术位置通过完整二元向量唯一找回真实基因/转录本、源行，保留原特征次序并修复样本标签。503 个源列的值为 FPKM>=1，NA 仍为 NA；这是表达存在指标，不能解释为 DNA 基因删除/存在，也不是论文按置信区间判断表达的原定义。
目标 last_juvenile_leaf（末片幼叶位置），取同研究 S6 的两年两重复汇总表型。380 个材料满足原始表头=S1 Genotype=S6 Genotype 的唯一精确匹配，并带 S1 accession。源文件两个 CM174 同名列使用 source_col 技术 ID 区分，均不配表型；其余无可靠映射/表型者也明确排除，不猜别名。技术 ID 不代表新增品种。使用已配对的 380 个材料完成分析。
按 epav_cv_folds.tsv 做五折 rrBLUP，训练折全缺失/常量列排除，其余训练均值插补和尺度变换。输出模型、预处理参数、折外预测、样本/特征清单、RMSE/MAE/Pearson 及散点图。原 2000 特征的历史抽样过程未完全复原，结论限定为给定特征集的预测功能，不宣称无偏生物学发现或论文复现。源坐标保持原语义；unplaced transcript 不是已定位染色体坐标。

C. SV genomic prediction（DATA/pangenome/sv_*）
输入 sv_matched.vcf、sv_genotype_matrix.tsv、sv_phenotype.tsv、sv_sample_map.tsv、sv_cv_folds.tsv、sv_excluded_samples.tsv。原 VCF 全部 500 条记录和 350 个样本表头与 Yang2025 官方 PopSV 压缩包逐条一致。本版取同研究 S1 中有 Seedling SR (%) 的 28 个精确匹配材料，目标 seedling_survival_percent、参考 B73v5。
GT 0/0、0/1、1/1 为非参考剂量 0、1、2；任一缺失等位基因记 NA，逐值核验预生成矩阵。独立按 sv_cv_folds.tsv 做五折 rrBLUP，训练折插补/尺度/筛选，不借 G2F/Hirsch 表型，也不跨两个群体拼接同名材料。输出每折模型、预处理、折外预测、样本/特征数、RMSE/MAE/Pearson、散点图。小样本及源面板亲缘结构限制必须报告，不据此推荐真实育种材料。
B/C 产物分别写 pangenome/epav、pangenome/sv 下的 sample_alignment.tsv、fold_audit.tsv、oof_predictions.tsv、metrics.tsv、每折模型及预测图，不能只交输入充分性审计。来源表只使用清单指定的 Hirsch S1/S6、Yang S1；不可读取未列明的论文 GWAS 结果或历史 benchmark 输出。
成功条件：A 的反应规范及两类验证、B/C 的五折预测全部实际完成，所有排除记录和数据限制可追溯。

交付共通要求：
在 OUT 写 report.md、reproduce.sh、stage_status.tsv、commands.json（包含软件版本、命令、退出码）。报告列明输入版本、实际使用/排除的样本和特征、参数、方法限制、运行状态；所有结果必须来自本次运行。reproduce.sh 使用绝对入口，重复执行不得破坏输入与历史结果。
```
