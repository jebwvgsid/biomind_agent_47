from pathlib import Path
import csv,json,shutil,hashlib
from xlsx_read import rows
R=Path(__file__).resolve().parents[1];D=R/'inputs';B=R.parent
notes={
'gwas':'''修订版 20261020-fullanalysis。正式性状为 phenotype.tsv（sample_id/C16_0），由原无表头教学文件原值转换。VCF、GFF 未改动。新版 Q 的215行身份绑定 Q_row_map.json/Q_sample_map.tsv 和源 FAM；不能继续使用旧无身份 Real_snp.3.Q。covariates.FID_IID_intercept_Q1_Q2.tsv 无表头、五列，按 TFAM ID 重排。K 必须由本次过滤 TPED 计算；本机 emmax-kin 实测输出 aBN.kinf。emmax-intel64 的 .ps 为 marker/beta/SE/p 四列。原教学参考组装 accession/性状单位未提供，题面保留来源限制。''',
'multiomics':'''修订版 20261020-fullanalysis，24样本功能分析。genotype64/expression60/metabolite24均按sample_id对齐，目标trait_multiomics_yield。omics_metadata是特征层表，不是样本表。正式折使用cv_fold_design；gwas_weight_fold_design的同号表示该样本所属的外层留出折，权重只从其补集重新计算，不是已执行GWAS结果。model_config明确三个原生模型的参数与训练要求，不能只交充分性审计。GP-WAITER输入8x8与卷积/注意力/输出头一起配置；原生网络拓扑保留，尺寸参数不同于论文大数据配置。三模型CPU可运行，参数10epochs用于此小样本功能题。没有提供隐藏真值、生成效应或历史评分。实际运行结果不作为正式输入。''',
'gxe':'''修订版 20261020-fullanalysis。895行正式表型都绑定原始小区Plot_ID，不能按sample×environment去重；拟合用组合的等权均值并保留n_plot。257行block原缺失不填造。天气来源为G2F2021，27环境通过官方映射连接24个天气标签；原小时/站点记录保留。weather_daily是同标签同日非缺失温度等权均值，window_coverage是DOY150..230的有效日均值及覆盖率。覆盖>=80%采用25环境；旧environment_timeseries只审计，不叠加。共享天气源按source_group整组留出；全缺失基因型标记必须在训练折内删除，其他插补/缩放只拟合训练数据。基因型分数保留为原给定数值特征；未重新验证亲本推导过程。窗口不是每个材料真实生育期。天气只作为GxE环境协变量。本版要完成反应规范及两类验证。''',
'pangenome':'''修订版 20261020-fullanalysis。旧矩阵解析错位，不能继续按旧样本列名配对。全部2000旧技术位置在公开原始FPKM按>=1二值化后唯一匹配，真实gene_id和源行见epav_features.tsv。重新解析508字段记录末尾的空字段，与507列表头对齐，恢复503源样本列；同名CM174两列仅用来源列技术ID区别，两者都不参与表型配对。NA保持NA。380个材料通过同研究Hirsch2014 S1与S6唯一精确匹配，目标末片幼叶位置。此矩阵是表达存在，不是DNA PAV。原特征选取历史未完全恢复，不能作无偏预测效果证明。SV原350样本/500记录已与Yang2025 PopSV逐字匹配；28个材料匹配同研究S1 Seedling SR%。两群体独立五折rrBLUP，不跨群拼接。来源证明与参考版本见provenance，正式表型来源只用列明表格。''',
'database':'''修订版 20261020-fullanalysis。query_input.json 给定查询主题、分页范围、链接获取上限、Ensembl ENSG00000142166 (IFNAR1, human)、限频及超时规则。实际元数据应联网重新检索，不把验收响应当本次结果；首次20条搜索结果不等于全库。无需用户补 gene ID。不得下载FASTQ。'''}
for k,s in notes.items():
 (D/k/'DATA_SCHEMA.txt').write_text(s+'\n')
 if (D/k/'provenance.json').exists():
  v=json.loads((D/k/'provenance.json').read_text());v['active_revision']='20261020-fullanalysis';v['revision_notes']=s;(D/k/'provenance.json').write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
# Provide only the relevant measured table from the multi-sheet Yang workbook.
src=B/'benchmarks/biomind_autotest_20261011_preflight/data_supplement_research_20261012/completion_v2/sources/nature_maize2025_tables.xlsx';data=rows(src,'S1');keys=sorted(set().union(*(x.keys() for x in data)),key=lambda x:(len(x),x))
with (D/'pangenome/sources/Yang2025_S1.tsv').open('w',newline='') as f:
 w=csv.writer(f,delimiter='\t');w.writerow(['source_excel_row']+keys);w.writerows([[i]+[x.get(k,'') for k in keys] for i,x in enumerate(data,1)])
p=D/'pangenome/sources/Yang2025_SupplementaryTables.xlsx'
if p.exists():p.unlink()
# Correct the descriptive old-column ledger using its actual, pandas-renamed labels.
old=B/'benchmarks/02.测试26.9/00.共享数据/fixtures_public/maize_pangenome_sv/pangenome_gene_presence_absence.features_by_sample.tsv'
with old.open() as f:oldhead=next(csv.reader(f,delimiter='\t'))
with (D/'pangenome/old_column_shift.tsv').open() as f:tab=list(csv.reader(f,delimiter='\t'))
for i,r in enumerate(tab[1:]):r[1]=oldhead[i]
with (D/'pangenome/old_column_shift.tsv').open('w',newline='') as f:csv.writer(f,delimiter='\t').writerows(tab)
# No hidden outcomes: explicit row memberships are deterministic input design only.
with (D/'multiomics/cv_fold_design.tsv').open() as f:cv=list(csv.DictReader(f,delimiter='\t'))
with (D/'multiomics/outer_fold_membership.tsv').open('w',newline='') as f:
 w=csv.writer(f,delimiter='\t');w.writerow(['outer_fold','sample_id','role']);w.writerows([[k,r['sample_id'],'test' if int(r['fold_id'])==k else 'train'] for k in range(1,6) for r in cv])
print('input notes finalized')
