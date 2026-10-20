from pathlib import Path
import pandas as pd,numpy as np,shutil,json,hashlib
R=Path(__file__).resolve().parents[1];B=R.parent;old=B/'benchmarks/03.输入修复26.10.20/00.共享数据';D=R/'inputs';D.mkdir(exist_ok=True)
for name in ['gwas','gxe','multiomics']:
 if not (D/name).exists():shutil.copytree(old/name,D/name,symlinks=True)
p=pd.read_csv(D/'gwas/Real_trait.C16_0.table',sep=r'\s+',header=None,names=['sample_id','C16_0']);assert len(p)==215 and not p.sample_id.duplicated().any();p.to_csv(D/'gwas/phenotype.tsv',sep='\t',index=False)
w=pd.read_csv(D/'gxe/source_weather_records.csv',dtype=str,keep_default_na=False);w['date']=pd.to_datetime(w[['Year','Month','Day']].rename(columns=str.lower)).dt.strftime('%Y-%m-%d');w['value']=pd.to_numeric(w['Temperature [C]'],errors='coerce');w['day']=pd.to_datetime(w.date).dt.dayofyear
# Weight each recorded time equally within day; then days equally in window. No row deletion/imputation.
daily=w.groupby(['Field Location','date','day'],sort=True)['value'].agg(['mean','count','size']).reset_index();daily.columns=['official_weather_label','date','day','Temperature_C','observed_readings','total_records'];daily.to_csv(D/'gxe/weather_daily.tsv',sep='\t',index=False,na_rep='NA')
m=pd.read_csv(D/'gxe/weather_source_map.tsv',sep='\t');sub=daily[daily.day.between(150,230)];stats=sub.groupby('official_weather_label').agg(observed_days=('Temperature_C','count'),window_mean_temperature=('Temperature_C','mean'));stats['expected_days']=81;stats['coverage']=stats.observed_days/81;stats['eligible']=stats.coverage.ge(.8)
q=m[['environment_id','official_weather_label','source_group']].merge(stats,on='official_weather_label',how='left');q.to_csv(D/'gxe/window_coverage.tsv',sep='\t',index=False,na_rep='NA')
# This table is derived environmental input, not a supervised model result.
pd.DataFrame([['calendar_doy_150_230',150,230,'Temperature_C',.8]],columns=['window_id','start_day','end_day','variable','min_day_coverage']).to_csv(D/'gxe/analysis_window.tsv',sep='\t',index=False)
(D/'database').mkdir(exist_ok=True)
query={'revision':'20261020-fullanalysis','organism':'Homo sapiens','geo_query':'Homo sapiens[Organism] AND PBMC AND interferon AND single cell','db':'gds','retmax':20,'retstart':0,'sort':'relevance','sra_fetch_limit_per_geo_uid':20,'pubmed_fetch_limit_per_geo_uid':5,'gene_stable_id':'ENSG00000142166','gene_symbol':'IFNAR1','species':'homo_sapiens','ensembl_endpoint':'https://rest.ensembl.org/lookup/id/ENSG00000142166?expand=0;content-type=application/json','assembly_policy':'record actual live response; do not hardcode coordinates','request_interval_seconds':.4,'timeout_seconds':30,'max_retries':2,'fastq_download':False}
(D/'database/query_input.json').write_text(json.dumps(query,indent=2)+'\n')
print(q.to_string(index=False));print('eligible environments',sum(q.eligible))
