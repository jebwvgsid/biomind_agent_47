# BM-REMAIN-CROP-ADVGP-010__strict

输入与任务修订：20261020-fullanalysis。此版本要求实际分析，历史输出与评分对应旧题，不适用于本版本。

```text
【Bioinformatics Agent Benchmark Test】
benchmark_id: BM-REMAIN-CROP-ADVGP-010
input_revision: 20261020-fullanalysis
ambiguity_level: strict
data_status: ready_verified
task_name: Native multiomics model training and held-out prediction

路径（绝对路径）：
PROJECT=/public/home/liusiqi/03.毕设/benchmarks/02.测试26.9/05.作物育种与多组学/04.BM-REMAIN-CROP-ADVGP-010__strict
DATA=/public/home/liusiqi/03.毕设/benchmarks/02.测试26.9/05.作物育种与多组学/04.BM-REMAIN-CROP-ADVGP-010__strict/data
OUT=/public/home/liusiqi/03.毕设/benchmarks/02.测试26.9/05.作物育种与多组学/04.BM-REMAIN-CROP-ADVGP-010__strict/output_inputfix_20261020
正式输入以 PROJECT/input_manifest.json 为准。data_inventory.tsv 与 input_status.tsv 是本版本有效清单；带旧日期后缀的清单只作为历史保存，不能补充本题输入。

执行要求：
Linux/Bash；按下文指定的绝对软件路径执行。输入只读，工作副本、索引、脚本、模型、表格、图片和日志全部写入 OUT；不写旧 output/ 或其他历史输出。不得读取历史结果、评判规则、隐藏答案或未列入清单的数据。无需安装 BioMind 或同名 skill；可使用指定原生模型与明示的等价实现。不得安装或修改共享软件环境。
只允许实际执行成功的阶段标记 completed；文件存在、软件安装或格式通过均不等于模型运行完成。遇到真实错误保存命令、退出码、stderr 和阶段原因，继续独立阶段。本版没有故意缺失的关键输入，也没有“仅交充分性审计即可”的成功条件。
全量大 VCF 分析通过 Slurm 申请资源；使用实际分配的额度。已核实 AMD_9A14 分区账户 zhangxingtan、QoS user_liusiqi，单核内存上限 3600 MB；例如 4 CPU/12 GB，记录实际作业 ID。队列变化时按实时错误记录，不将调度失败写成数据缺失。
固定随机种子 20260603。不同版本的性能、结果和评分不可直接混用。

目标：对同一份已对齐的模拟多组学输入，实际训练 GP-WAITER、Cropformer、DNNGP 并完成五折折外预测。它是 24 个样本的功能测试，不是论文性能复现，也不能据此宣称育种价值。
正式输入 DATA/multiomics/：genotype_matrix.tsv（sample_id + 64 个 0/1/2 剂量）、expression_matrix.tsv（60 特征）、metabolite_matrix.tsv（24 特征）、phenotype.tsv（trait_multiomics_yield）、omics_metadata.tsv（特征层元数据，不是样本表）、cv_fold_design.tsv、gwas_weight_fold_design.tsv、model_config.json。各矩阵行是样本，列是特征；按 sample_id 明确连接，特征顺序固定为各文件表头。不得读取模拟真值或隐藏因果标签。
原生模块及环境：
- GP-WAITER：/public/home/liusiqi/03.毕设/benchmarks/runtime_setup_20261014/output/envs/gp_waiter/bin/python；原模块 /public/home/liusiqi/03.毕设/benchmarks/runtime_setup_20261014/output/sources/GP-WAITER/model/GP_WAITER.cpython-39-x86_64-linux-gnu.so。
- Cropformer：/public/home/liusiqi/03.毕设/benchmarks/runtime_setup_20261014/output/envs/cropformer/bin/python；入口 /public/home/liusiqi/03.毕设/benchmarks/runtime_setup_20261014/output/entrypoints/Cropformer/model.py 的 SelfAttention（该副本只补齐缺失 nn 导入）。
- DNNGP：/public/home/liusiqi/03.毕设/benchmarks/runtime_setup_20261014/output/envs/dnngp_tf26/bin/python；/public/home/liusiqi/03.毕设/benchmarks/runtime_setup_20261014/output/sources/DNNGP/Scripts/dnngp.cpython-39-x86_64-linux-gnu.so 中 dnngp_model。
本版可配置训练入口 PROJECT/analysis_support/train_multiomics.py：分别用对应环境调用 --model gp_waiter|cropformer|dnngp --data DATA/multiomics --config DATA/multiomics/model_config.json --out OUT/对应模型。参数必须绑定为本题绝对路径，不将文字占位符直接传给 shell。入口可在 OUT 中复制修改，保留具体 diff 与参数；不得用 sklearn 简化模型替代原生网络。
1. 核对所有 ID、特征层、剂量、缺失、重复与折；按 cv_fold_design 的 fold_id=1..5 原样划分。每个样本必须且只能获得一次折外预测。外层测试标签不参与任何插补、缩放、权重、特征选择、调参和早停。
2. 目标固定 trait_multiomics_yield。每折输入均值/标准差只用实际训练样本拟合；标准差接近 0 的列用 scale=1；缺值用训练均值，训练中全缺失的列明确排除并记录。y 只用训练均值/标准差缩放，预测后还原原尺度。
3. GP-WAITER 使用原生 TModel 与固定 model_config。64 个真实标记按原顺序 reshape 8×8，无填充/复制标记。四层 Conv2d 核为 3×3、3×3、1×1、1×1；输出 4×4，三层注意力维数为 4→8→8→8，头数 1、2、2；保留原生两层输出 Conv1d 与激活，核/步长配置为 2/1、3/1，将 4 个位置缩为 1。是保持原生网络结构的小尺寸配置，不等同论文 180×4589 参数规模。只在外层训练折逐标记拟合单变量线性关联，权重 w=1+min(10,-log10(max(p,1e-10)))，常量列 p=1；每折保存权重、marker 顺序和训练 ID。此处为模拟输入的训练折关联权重，不宣称已完成控制群体结构的正式 GWAS。
4. Cropformer 用原生 SelfAttention，input_size=64、hidden_size=32、heads=4、kernel_size=3、两类 dropout=0.2。
5. GP-WAITER 与 Cropformer 固定 Adam lr=0.001、10 epochs、全训练折小批次、seed=20260603+fold，禁止根据外层测试损失选择 epoch。本配置支持 CPU，无 GPU 也可完整运行；若显式设置 CUDA 必须使用已分配设备。原生 GP 的 squeeze 在单样本批次会丢维，入口以同一输入张量的计算副本避免此问题，并仅返回一条预测；不产生额外生物样本，要求单条与批量推理一致。
6. DNNGP 拼接 genotype→expression→metabolite 共 148 特征。外层训练 ID 按 SHA256('20260603|fold|sample_id') 排序，前 max(3,ceil(0.2*n_train)) 为内层验证，其余实际训练；所有缩放用实际训练子集。调用原生 dnngp_model，外层测试不传入该函数。固定 10 epochs、batch=4、lr=0.001、dropout1/2=0.2、patience=20、earlystopping=40；内层验证用于原生训练监控，训练后加载原生输出 .h5 对封存的外层测试预测。保存实际 checkpoint、训练历史与 fold_audit。
产物：input_qc.tsv、sample_alignment.tsv、每模型五折 model.pt 或 .h5、preprocessing.npz、fold_audit.json、training_history.tsv、oof_predictions.tsv（sample_id/fold_id/observed/predicted）、模型比较表（相同外层样本上的 RMSE/MAE/Pearson；常量预测相关系数记 NA）、预测散点图。报告明确 DNNGP 使用内层验证、三模型训练样本量/输入层不同；小样本指标只作为本数据划分的结果。成功条件是三个模型全部实际训练和预测，不能只交“可安装/可路由”的说明。

交付共通要求：
在 OUT 写 report.md、reproduce.sh、stage_status.tsv、commands.json（包含软件版本、命令、退出码）。报告列明输入版本、实际使用/排除的样本和特征、参数、方法限制、运行状态；所有结果必须来自本次运行。reproduce.sh 使用绝对入口，重复执行不得破坏输入与历史结果。
```
