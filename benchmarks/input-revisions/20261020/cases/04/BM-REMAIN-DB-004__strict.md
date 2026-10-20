# BM-REMAIN-DB-004__strict

输入与任务修订：20261020-fullanalysis。此版本要求实际分析，历史输出与评分对应旧题，不适用于本版本。

```text
【Bioinformatics Agent Benchmark Test】
benchmark_id: BM-REMAIN-DB-004
input_revision: 20261020-fullanalysis
ambiguity_level: strict
data_status: ready_verified
task_name: Live public database discovery and gene metadata retrieval

路径（绝对路径）：
PROJECT=/public/home/liusiqi/03.毕设/benchmarks/02.测试26.9/03.序列检索与数据库/04.BM-REMAIN-DB-004__strict
DATA=/public/home/liusiqi/03.毕设/benchmarks/02.测试26.9/03.序列检索与数据库/04.BM-REMAIN-DB-004__strict/data
OUT=/public/home/liusiqi/03.毕设/benchmarks/02.测试26.9/03.序列检索与数据库/04.BM-REMAIN-DB-004__strict/output_inputfix_20261020
正式输入以 PROJECT/input_manifest.json 为准。data_inventory.tsv 与 input_status.tsv 是本版本有效清单；带旧日期后缀的清单只作为历史保存，不能补充本题输入。

执行要求：
Linux/Bash；按下文指定的绝对软件路径执行。输入只读，工作副本、索引、脚本、模型、表格、图片和日志全部写入 OUT；不写旧 output/ 或其他历史输出。不得读取历史结果、评判规则、隐藏答案或未列入清单的数据。无需安装 BioMind 或同名 skill；可使用指定原生模型与明示的等价实现。不得安装或修改共享软件环境。
只允许实际执行成功的阶段标记 completed；文件存在、软件安装或格式通过均不等于模型运行完成。遇到真实错误保存命令、退出码、stderr 和阶段原因，继续独立阶段。本版没有故意缺失的关键输入，也没有“仅交充分性审计即可”的成功条件。
全量大 VCF 分析通过 Slurm 申请资源；使用实际分配的额度。已核实 AMD_9A14 分区账户 zhangxingtan、QoS user_liusiqi，单核内存上限 3600 MB；例如 4 CPU/12 GB，记录实际作业 ID。队列变化时按实时错误记录，不将调度失败写成数据缺失。
固定随机种子 20260603。不同版本的性能、结果和评分不可直接混用。

目标：完成 human PBMC interferon stimulation scRNA-seq 的 GEO→SRA/PubMed 元数据检索，并查询人类 IFNAR1 的明确 Ensembl gene stable ID。
正式输入 DATA/database/query_input.json，明确 gene_stable_id=ENSG00000142166、species=homo_sapiens。该 ID 是本题给定查询对象；坐标、组装版本和数据库版本须从本次响应读取。
环境：/public/home/liusiqi/anaconda3/envs/biomind/bin/python；urllib/requests、JSON/XML 标准解析即可。公共 API 不要求提供 API key；不得把密钥写入日志或报告。
1. NCBI ESearch db=gds、term="Homo sapiens[Organism] AND PBMC AND interferon AND single cell"、retmode=json、retmax=20、retstart=0、sort=relevance。保存实际编码 URL、Count、返回 UID。任务范围明确为本次第一页最多 20 条，不把本页说成全部数据库记录。
2. ESummary 对真实 UID 获取 GSE/GSM、标题、物种、实验类型、研究描述；逐条判断是否真正支持 PBMC、干扰素刺激和单细胞 RNA 测序，区分 confirmed/candidate/excluded 与证据字段。不因为关键词命中就断言全部适合。
3. 对每个本页 UID 分别 ELink dbfrom=gds→sra 和 pubmed；先保存完整关联 ID 列表，再分别取数值 ID 升序前 20 个 SRA、前 5 个 PubMed 做 EFetch XML。逐 UID 报告关联总数、获取数、是否截断。无关联写 no_links 和空表，不伪造 accession；不能在 search/link 前编造 fetch ID。SRA Run accession 只能解析真实 XML。GSE/GSM 到 SRA/PubMed 的连接均保留源 UID，不做无依据串联。
4. GET https://rest.ensembl.org/lookup/id/ENSG00000142166?expand=0;content-type=application/json，Accept: application/json；核验返回 id/species/object_type，输出 display_name、description、seq_region_name、start、end、strand、assembly_name、version 与请求时间，不预填坐标。
5. 无 key 的 NCBI 请求串行间隔至少 0.4 秒，timeout=30 秒；瞬时失败最多重试 2 次并退避，保留 HTTP 状态与错误。服务不可用标记具体阶段网络错误，继续其他 API；禁止 FASTQ 或其他大规模测序下载。
产物：query_log.tsv（时间、URL/参数、HTTP、字节数、返回数、尝试次数）、raw_responses/（实际 HTTP 正文）、geo_candidate_table.tsv、sra_run_mapping.tsv、pubmed_evidence.tsv、retrieval_scope.tsv、ensembl_gene_summary.json。成功条件：四个 API 操作阶段均完成或真实返回无关联；仅计划查询不算已完成。

交付共通要求：
在 OUT 写 report.md、reproduce.sh、stage_status.tsv、commands.json（包含软件版本、命令、退出码）。报告列明输入版本、实际使用/排除的样本和特征、参数、方法限制、运行状态；所有结果必须来自本次运行。reproduce.sh 使用绝对入口，重复执行不得破坏输入与历史结果。
```
