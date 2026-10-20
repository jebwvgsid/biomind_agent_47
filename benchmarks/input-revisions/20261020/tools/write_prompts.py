from pathlib import Path
import json
R=Path(__file__).resolve().parents[1];B=R.parent;cases=json.loads((R/'evidence/original-cases.json').read_text());RUNTIME=B/'benchmarks/runtime_setup_20261014/output';GWAS=B/'benchmarks/biomind_autotest_20261011_preflight/dependency_plan/proposed-envs/bio_gwas_cli';RR=B/'benchmarks/biomind_autotest_20261011_preflight/dependency_plan/proposed-envs/bio_crop_gs_r43'
def emit(i,title,body):
 c=cases[i-1];P=Path(c['project']);out=P/'output_inputfix_20261020';stage=R/'staged'/f'{i:02d}';stage.mkdir(exist_ok=True,parents=True)
 text=f'''# {c['case_id']}

输入与任务修订：20261020-fullanalysis。此版本要求实际分析，历史输出与评分对应旧题，不适用于本版本。

```text
【Bioinformatics Agent Benchmark Test】
benchmark_id: {c['case_id'].removesuffix('__strict')}
input_revision: 20261020-fullanalysis
ambiguity_level: strict
task_name: {title}

路径（绝对路径）：
PROJECT={P}
DATA={P/'data'}
OUT={out}
正式输入以 PROJECT/input_manifest.json 为准。data_inventory.tsv 与 input_status.tsv 是本版本有效清单；带旧日期后缀的清单只作为历史保存，不能补充本题输入。

执行要求：
Linux/Bash；按下文指定的绝对软件路径执行。输入只读，工作副本、索引、脚本、模型、表格、图片和日志全部写入 OUT；不写旧 output/ 或其他历史输出。不得读取历史结果、评判规则、隐藏答案或未列入清单的数据。无需安装 BioMind 或同名 skill；可使用指定原生模型与明示的等价实现。不得安装或修改共享软件环境。
只允许实际执行成功的阶段标记 completed；文件存在、软件安装或格式通过均不等于模型运行完成。遇到真实错误保存命令、退出码、stderr 和阶段原因，继续独立阶段。本版没有故意缺失的关键输入，也没有“仅交充分性审计即可”的成功条件。
全量大 VCF 分析通过 Slurm 申请资源；使用实际分配的额度。已核实 AMD_9A14 分区账户 zhangxingtan、QoS user_liusiqi，单核内存上限 3600 MB；例如 4 CPU/12 GB，记录实际作业 ID。队列变化时按实时错误记录，不将调度失败写成数据缺失。
固定随机种子 20260603。不同版本的性能、结果和评分不可直接混用。

{body}

交付共通要求：
在 OUT 写 report.md、reproduce.sh、stage_status.tsv、commands.json（包含软件版本、命令、退出码）。报告列明输入版本、实际使用/排除的样本和特征、参数、方法限制、运行状态；所有结果必须来自本次运行。reproduce.sh 使用绝对入口，重复执行不得破坏输入与历史结果。
```
'''
 (stage/(c['case_id']+'.md')).write_text(text)

gwas=f'''目标：以 215 个样本的 C16_0 性状完成 EMMAX MLM GWAS、绘图、显著位点、候选区及真实 LD 分析。
正式输入位于 DATA/gwas/：Real_snp.vcf.gz、phenotype.tsv、Real.gff3、Real_snp.regenerated.3.Q、Q_row_map.json、Q_sample_map.tsv、Q_with_sample_ids.tsv、formal_samples.tfam、covariates.FID_IID_intercept_Q1_Q2.tsv、vcf_to_plink_chromosome.tsv。原 Real_trait.C16_0.table 是无表头两列文件，phenotype.tsv 为其无数值改动的带表头版本，列 sample_id、C16_0；不得从不存在的原表头猜测性状。单位原教学数据未说明，报告保留原尺度，不擅自赋予单位。
软件：{GWAS}/bin/plink、emmax-kin-intel64、emmax-intel64；绘图 {B}/anaconda3_not_used（此路径不作为入口）；实际 Rscript 为 /public/home/liusiqi/anaconda3/envs/rna_env/bin/Rscript。表格处理用 /public/home/liusiqi/anaconda3/envs/biomind/bin/python。EMMAX 不可用时不能替换成 TASSEL、普通回归或其他模型。
1. 检查 VCF 完整性、二倍体 GT、样本唯一性、变异数、缺失率、染色体与 GFF 坐标标签。保留输入校验和。用 PLINK --vcf 指定文件 --double-id --maf 0.05 --geno 0.1 --recode 12 transpose --output-missing-genotype 0 --set-missing-var-ids @:# --allow-extra-chr --keep-allele-order，输出 OUT/prep/markers。额外染色体名按 vcf_to_plink_chromosome.tsv 在工作 TPED 副本中转换为整数；保留原 CHR/POS/marker 对照，注释与作图时恢复。检测 marker ID 重复，必要时给冲突记录加 REF/ALT 以确保唯一并保存映射。
2. 严格按 TFAM 的 FID/IID 显式连接 phenotype 和新版 Q 的 sample_id，不以长度相同代替身份。Q_row_map.json 绑定新版 Q 与生成时 FAM 的校验和；核对后按 ID 重排。新 Q 是 ADMIXTURE K=3、seed=20261012 的重建输入，不声称恢复旧无身份 Q。共变量使用 FID IID intercept Q1 Q2 五列，无表头；Q3 与截距冗余，保留在原始 Q 但不加入模型。检查共变量秩为 3。
3. 对实际过滤后的 TPED 用 emmax-kin-intel64 -v -d 10 计算 K。运行可生成 .aBN.kinf，必须按实际生成文件名取 K，不能假定 .aIBS.kinf 或使用日志。EMMAX -v -d 10 -t TPED前缀 -p 三列表型 -k 实际K -c 五列共变量 -o OUT/gwas/emmax。表型为 FID IID C16_0，无表头，保持 TFAM 顺序。
4. 本机 EMMAX .ps 实际四列为 marker、beta、SE、p；按 marker 联接 TPED，不能误用 SE 作 p。检查有效 p∈(0,1]，记录无效记录原因，M 为实际有效检验数，阈值 0.05/M。绘制所有有效 p 的 Manhattan/QQ，导出 gwas/gwas_results.tsv、gwas/manhattan_input.tsv、plots/manhattan.png、plots/qqplot.png、significant_sites.tsv。
5. 显著位点同染色体 ±50 kb 合并为候选区，与 Real.gff3 基因区间相交，输出 ldblock_candidate_regions.tsv、candidate_genes.tsv。使用同一批保留样本、真实基因型在候选区计算成对 r²，明确其为局部 LD 结果；±50 kb 窗口本身不等于统计 LD block。无显著位点时写带表头的空候选表和 no-hit 说明，属于有效结果，不降低阈值凑结果。原 GFF 未提供可验证公共组装 accession，不外推到其他棉花参考版本。
成功条件：全量正式 VCF 完成 QC、K、EMMAX、图和候选/LD 阶段；小规模验收不能代替本题全量分析。'''
gwas=gwas.replace(f'绘图 {B}/anaconda3_not_used（此路径不作为入口）；实际 Rscript 为','绘图 Rscript 为')
emit(1,'Population-genetics GWAS complete analysis',gwas)

multi=f'''目标：对同一份已对齐的模拟多组学输入，实际训练 GP-WAITER、Cropformer、DNNGP 并完成五折折外预测。它是 24 个样本的功能测试，不是论文性能复现，也不能据此宣称育种价值。
正式输入 DATA/multiomics/：genotype_matrix.tsv（sample_id + 64 个 0/1/2 剂量）、expression_matrix.tsv（60 特征）、metabolite_matrix.tsv（24 特征）、phenotype.tsv（trait_multiomics_yield）、omics_metadata.tsv（特征层元数据，不是样本表）、cv_fold_design.tsv、gwas_weight_fold_design.tsv、model_config.json。各矩阵行是样本，列是特征；按 sample_id 明确连接，特征顺序固定为各文件表头。不得读取模拟真值或隐藏因果标签。
原生模块及环境：
- GP-WAITER：{RUNTIME}/envs/gp_waiter/bin/python；原模块 {RUNTIME}/sources/GP-WAITER/model/GP_WAITER.cpython-39-x86_64-linux-gnu.so。
- Cropformer：{RUNTIME}/envs/cropformer/bin/python；入口 {RUNTIME}/entrypoints/Cropformer/model.py 的 SelfAttention（该副本只补齐缺失 nn 导入）。
- DNNGP：{RUNTIME}/envs/dnngp_tf26/bin/python；{RUNTIME}/sources/DNNGP/Scripts/dnngp.cpython-39-x86_64-linux-gnu.so 中 dnngp_model。
本版可配置训练入口 PROJECT/analysis_support/train_multiomics.py：分别用对应环境调用 --model gp_waiter|cropformer|dnngp --data DATA/multiomics --config DATA/multiomics/model_config.json --out OUT/对应模型。参数必须绑定为本题绝对路径，不将文字占位符直接传给 shell。入口可在 OUT 中复制修改，保留具体 diff 与参数；不得用 sklearn 简化模型替代原生网络。
1. 核对所有 ID、特征层、剂量、缺失、重复与折；按 cv_fold_design 的 fold_id=1..5 原样划分。每个样本必须且只能获得一次折外预测。外层测试标签不参与任何插补、缩放、权重、特征选择、调参和早停。
2. 目标固定 trait_multiomics_yield。每折输入均值/标准差只用实际训练样本拟合；标准差接近 0 的列用 scale=1；缺值用训练均值，训练中全缺失的列明确排除并记录。y 只用训练均值/标准差缩放，预测后还原原尺度。
3. GP-WAITER 使用原生 TModel 与固定 model_config。64 个真实标记按原顺序 reshape 8×8，无填充/复制标记。四层 Conv2d 核为 3×3、3×3、1×1、1×1；输出 4×4，三层注意力维数为 4→8→8→8，头数 1、2、2；保留原生两层输出 Conv1d 与激活，核/步长配置为 2/1、3/1，将 4 个位置缩为 1。是保持原生网络结构的小尺寸配置，不等同论文 180×4589 参数规模。只在外层训练折逐标记拟合单变量线性关联，权重 w=1+min(10,-log10(max(p,1e-10)))，常量列 p=1；每折保存权重、marker 顺序和训练 ID。此处为模拟输入的训练折关联权重，不宣称已完成控制群体结构的正式 GWAS。
4. Cropformer 用原生 SelfAttention，input_size=64、hidden_size=32、heads=4、kernel_size=3、两类 dropout=0.2。
5. GP-WAITER 与 Cropformer 固定 Adam lr=0.001、10 epochs、全训练折小批次、seed=20260603+fold，禁止根据外层测试损失选择 epoch。本配置支持 CPU，无 GPU 也可完整运行；若显式设置 CUDA 必须使用已分配设备。原生 GP 的 squeeze 在单样本批次会丢维，入口以同一输入张量的计算副本避免此问题，并仅返回一条预测；不产生额外生物样本，要求单条与批量推理一致。
6. DNNGP 拼接 genotype→expression→metabolite 共 148 特征。外层训练 ID 按 SHA256('20260603|fold|sample_id') 排序，前 max(3,ceil(0.2*n_train)) 为内层验证，其余实际训练；所有缩放用实际训练子集。调用原生 dnngp_model，外层测试不传入该函数。固定 10 epochs、batch=4、lr=0.001、dropout1/2=0.2、patience=20、earlystopping=40；内层验证用于原生训练监控，训练后加载原生输出 .h5 对封存的外层测试预测。保存实际 checkpoint、训练历史与 fold_audit。
产物：input_qc.tsv、sample_alignment.tsv、每模型五折 model.pt 或 .h5、preprocessing.npz、fold_audit.json、training_history.tsv、oof_predictions.tsv（sample_id/fold_id/observed/predicted）、模型比较表（相同外层样本上的 RMSE/MAE/Pearson；常量预测相关系数记 NA）、预测散点图。报告明确 DNNGP 使用内层验证、三模型训练样本量/输入层不同；小样本指标只作为本数据划分的结果。成功条件是三个模型全部实际训练和预测，不能只交“可安装/可路由”的说明。'''
emit(2,'Native multiomics model training and held-out prediction',multi)
db='''目标：完成 human PBMC interferon stimulation scRNA-seq 的 GEO→SRA/PubMed 元数据检索，并查询人类 IFNAR1 的明确 Ensembl gene stable ID。
正式输入 DATA/database/query_input.json，明确 gene_stable_id=ENSG00000142166、species=homo_sapiens。该 ID 是本题给定查询对象；坐标、组装版本和数据库版本须从本次响应读取。
环境：/public/home/liusiqi/anaconda3/envs/biomind/bin/python；urllib/requests、JSON/XML 标准解析即可。公共 API 不要求提供 API key；不得把密钥写入日志或报告。
1. NCBI ESearch db=gds、term="Homo sapiens[Organism] AND PBMC AND interferon AND single cell"、retmode=json、retmax=20、retstart=0、sort=relevance。保存实际编码 URL、Count、返回 UID。任务范围明确为本次第一页最多 20 条，不把本页说成全部数据库记录。
2. ESummary 对真实 UID 获取 GSE/GSM、标题、物种、实验类型、研究描述；逐条判断是否真正支持 PBMC、干扰素刺激和单细胞 RNA 测序，区分 confirmed/candidate/excluded 与证据字段。不因为关键词命中就断言全部适合。
3. 对每个本页 UID 分别 ELink dbfrom=gds→sra 和 pubmed；先保存完整关联 ID 列表，再分别取数值 ID 升序前 20 个 SRA、前 5 个 PubMed 做 EFetch XML。逐 UID 报告关联总数、获取数、是否截断。无关联写 no_links 和空表，不伪造 accession；不能在 search/link 前编造 fetch ID。SRA Run accession 只能解析真实 XML。GSE/GSM 到 SRA/PubMed 的连接均保留源 UID，不做无依据串联。
4. GET https://rest.ensembl.org/lookup/id/ENSG00000142166?expand=0;content-type=application/json，Accept: application/json；核验返回 id/species/object_type，输出 display_name、description、seq_region_name、start、end、strand、assembly_name、version 与请求时间，不预填坐标。
5. 无 key 的 NCBI 请求串行间隔至少 0.4 秒，timeout=30 秒；瞬时失败最多重试 2 次并退避，保留 HTTP 状态与错误。服务不可用标记具体阶段网络错误，继续其他 API；禁止 FASTQ 或其他大规模测序下载。
产物：query_log.tsv（时间、URL/参数、HTTP、字节数、返回数、尝试次数）、raw_responses/（实际 HTTP 正文）、geo_candidate_table.tsv、sra_run_mapping.tsv、pubmed_evidence.tsv、retrieval_scope.tsv、ensembl_gene_summary.json。成功条件：四个 API 操作阶段均完成或真实返回无关联；仅计划查询不算已完成。'''
emit(4,'Live public database discovery and gene metadata retrieval',db)
print('staged prompts 1,2,4')
