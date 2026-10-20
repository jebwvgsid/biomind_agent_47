from pathlib import Path
import json,csv,hashlib,os
R=Path(__file__).resolve().parents[1];m=json.loads((R/'manifest.json').read_text());audit=json.loads((R/'evidence/final-audit.json').read_text());vcf=json.loads((R/'evidence/full-vcf-integrity.json').read_text()) if (R/'evidence/full-vcf-integrity.json').exists() else None
lines=['原四题输入修订与验收交付（20261020-fullanalysis）','',
'已按用户选择将四题改为各阶段实际分析题，并修改原题目录中的题面、data 入口、有效输入清单和输入状态。保留同一 case_id，但新 input_revision 与题面 SHA256 独立标记；旧评分不能用于本版。',
'本轮没有运行新一轮智能体正式评分，也没有部署 BioMind 生产软件。原提交包的开发/联网工作交付仍在 BioMind_Submission_Work_20261020/result；本目录是原题输入与题面修订的新增交付。','',
'实际修改位置：']
for t in m['tasks']:lines+=['- '+t['case_id'], '  题面：'+t['prompt_source'],'  输入：'+str(Path(t['project_dir'])/'data'),'  新分析输出：'+t['output_directory']]
lines+=['','运行与版本清单：'+str(R/'manifest.json'),'输入来源与新数据：'+str(R/'inputs'),'完整备份：'+str(R/'backup'),'精确题面及训练入口差异：'+str(R/'review'),'','主要修复与验收：',
'1. GWAS：215 行新版 Q 与原 FAM 身份绑定，带表头的 phenotype.tsv 与原无表头性状原值一致。补全 Chr01..Chr14 到 PLINK 的显式映射；旧 ADMIXTURE 编码仅 1..13，不能作为正式 VCF 的完整映射。实际 5000 条原始变异经 QC 保留 3144 条，PLINK→真实 kinship（aBN.kinf）→EMMAX 四列输出→Manhattan/QQ 均成功；另对 30 个真实标记计算 435 对 LD。全文件核实 Chr14 仅在头部声明，没有变异记录，因此 GFF 仅含 Chr01..Chr13 不构成当前变异的注释缺口；原教学组装 accession/性状单位仍未给出，不外推参考版本。全量科学 GWAS 尚未重跑，子集结果不作为全量显著性结论。',
'2. 多组学：24 个模拟样本、64 SNP+60 expression+24 metabolite。三种原生模型均执行 5 折、每折 10 epochs，分别得到 24 条 OOF 预测。训练/内层验证/外层测试成员互斥；权重和缩放只拟合对应训练成员。保留每折模型、训练历史、预处理与成员证据。小样本验证只证明输入和执行流程可用，不是论文效果复现。',
'   GP-WAITER 原作者训练脚本将其数据固定 reshape 为 180×4589，并硬编码配套 CNN/attention 尺寸。这是脚本实现限制，不是方法必须要求。新的正式训练入口 analysis_support/train_multiomics.py 使用 model_config.json，当前 64 标记采用 8×8，卷积、注意力和输出卷积尺寸成套配置；保留原生 TModel 操作拓扑，调整尺寸参数，CPU 可运行。修复原生单样本 squeeze 的调用问题；单条和批量推理已验证一致。原版源文件保留用于对照。',
'3. GxE：895 小区记录按 Plot_ID 与源行绑定，非重复导入；按 sample×environment 聚合为模型观测。27 环境映射至官方天气源，窗口 DOY150..230，25 环境达到 >=80% 日覆盖，IAH3/WIH1 排除并保留原因；共享源形成 22 组。31 个材料反应规范及 360 个样本×环境观测的留天气组/留基因型验证均成功。修复了全缺失标记均值 NaN 和 R 标量/矩阵相加的执行问题。天气在这里是作物 GxE 的环境协变量，不是另加的气象测试。',
'4. 原 PAV 的实质错误：公开 FPKM 源表每条记录末尾多出空字段，旧 pandas 解析导致基因 ID 丢失、坐标当样本、样本列错移。旧标记 A797NW 的值实际来自 B73。原始 152172120 字节文件与公开 MD5 一致，全部 2000 个旧位置按 FPKM>=1 的完整向量唯一匹配到源行；重新解析恢复真实基因/转录本与503源列，NA保留。原源表两列同名 CM174 无法可靠区分，赋源列技术 ID 并排除表型连接；没有伪造新材料。与同论文 S1/S6 精确配对后，380 个材料完成五折 rrBLUP。这里是表达存在特征，不是 DNA 基因删除，题面已纠正科学含义。原特征抽样历史未完全复原，不声称无偏性能评估。',
'5. SV：现有 VCF 的350样本表头与全部500条变异记录逐条匹配官方 PopSV 压缩包。经同论文 S1 的实测幼苗存活率配对，28 个材料/500 个 SV 完成独立五折 rrBLUP。没有借用另一群体的表型；没有复用先前未经证实的同名配对。',
'6. 数据库题：正式 query_input.json 给定 human IFNAR1 的 ENSG00000142166，补齐原题缺 ID 的断点。已实际运行 GEO搜索/summary、真实UID到SRA/PubMed的link/fetch，以及Ensembl响应核验。分页范围和每UID获取上限写明，未下载FASTQ。验收仅验证首搜索页与各库真实关联获取；正式题仍须按本版范围完整检索，不把验收缓存当结果。','']
if vcf:lines+=['全量 VCF 完整性检查：'+json.dumps(vcf,ensure_ascii=False),'']
else:lines+=['全量 VCF 完整性检查仍在 Slurm 运行；此报告将于作业完成后更新。','']
lines+=['保留与限制：',f'- 对 {audit["historical_outputs_preserved"]} 个历史输出文件核对 size/mtime/inode/符号链接目标，全部未改。旧 data 树另存各原题 data_before_inputfix_20261020，旧题面/清单保存在 backup。历史评分未改。',f'- 最终结构/成员/版本审计 {audit["n_checks"]} 项通过，{audit["unique_input_files"]} 个唯一正式输入或入口文件。',
'- 尚未重跑全量 GWAS、正式智能体 benchmark 或新评分；不得把 bounded native validation 写成全量题目完成。',
'- 生物学限制保留：教学性状单位/公共组装 accession 未给出；GxE 的亲本推导数值未独立重建；PAV 源特征抽样过程未完全复原；小样本 SV/模拟深度模型指标不支持真实育种性能结论。它们不再是缺少表型、ID、输入形状或训练目标造成的阻断。',
'- API 与调度资源是运行时依赖，本次已实际验证，未来服务变化应按题面记录具体错误。','',
'来源证据：',
'- Hirsch2014 论文 DOI 10.1105/tpc.113.119982；补充 Dataset S1/S6 取自 Oxford 官方文章附件。',
'- FPKM： https://zenodo.org/record/4994698/files/maize_503genotypes_raw_expression_values_FPKM.txt ，MD5 c81d34d50e46574785fa0112a3286f3b；Dryad DOI 10.5061/dryad.r73c5。',
'- Yang2025： https://www.nature.com/articles/s41588-025-02378-w ；PopSV https://zenodo.org/record/16576184/files/PopSV.zip 。仅下载16MB前缀，足够解压和核对既有500完整记录；不声称校验了整个1.1GB压缩包。',
'- 初次 Dryad/Zenodo 路由的401/403、最终公开路由成功记录均保留，没有通过账号或规避认证。','',
'关键证据文件：',
str(R/'evidence/final-audit.json'),str(R/'evidence/pav-recovery.json'),str(R/'evidence/sv-source-match.json'),str(R/'evidence/native-cv'),str(R/'evidence/gxe-verified'),str(R/'evidence/pangenome-native'),str(R/'evidence/gwas-native'),str(R/'evidence/database-live'),'',
'生产待审范围：本轮新增训练适配入口及输入/题面差异见 review；原 GP-WAITER/Cropformer/DNNGP 上游模块与共享环境没有改动。正式原题已更新输入，BioMind 生产版本没有部署变更。']
(R/'交付与验收说明.txt').write_text('\n'.join(lines)+'\n')
(R/'review/README.txt').write_text('本轮原题题面与训练入口的可审查差异。四个题面 patch 对比 backup 中原版；gp_waiter_training_entrypoint.patch 对比上游硬编码训练驱动与本题实际采用的新入口。新的三模型入口不修改共享上游源模块；GP 保留原生操作拓扑但改变尺寸超参数。本轮没有部署 BioMind 生产版本，也没有运行正式评分。输入的逐文件校验和、来源与输出版本见上级 manifest.json 和各题 input_manifest.json。\n')
print(R/'交付与验收说明.txt')
