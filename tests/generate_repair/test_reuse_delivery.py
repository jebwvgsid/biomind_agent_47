"""Synthetic complete chain and immutable historical evidence are separate cases."""
import asyncio
from copy import deepcopy
import hashlib,json,os,shutil
from pathlib import Path
from unittest.mock import patch
import pytest
from tests.react_v3.support import task_fixture
from tests.generate_repair.test_correctness import install_receipt,sample_plan,write_samples
from agent_core.plan_tracker import load_plan_state,save_plan_state,save_execution_plan
from agent_core.work_reuse import begin_result_review,write_review_deliverables
from agent_core.request_state import request_scope
from agent_core.completion_gate import verify_work_result
from agent_core.delivery_files import render_delivery,usable_gate,reproduction_script,main

F=Path(__file__).parent/'fixtures/incident'

def record(name,data):
 p=Path(os.environ['BIOMIND_OFFLINE_RESULTS']).parent/(name+'.json');p.write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str)+'\n')

def plan8(f):
 p=sample_plan(f);p['path_bindings'].update(out_root=str(f.root),skill_scripts_dir='/registered/skill/scripts')
 p['runtime_binding']={'launcher':'/registered/python -u','decision':'registry_reused'}
 p['output_contracts']=[{'artifact_id':ref,'role':ref,'format':fmt,'path':str(f.root/path)} for ref,fmt,path in [
  ('matrix_balance_report','tsv','mcool_balance_preflight.tsv'),('tad_domains','bed','40000/{sample}_domains.bed'),('tad_boundaries','bed','40000/{sample}_boundaries.bed'),('tad_summary','tsv','40000/tad_domain_stats.tsv'),('report','md','report.md'),('reproduce_script','sh','reproduce.sh'),('stage_status','tsv','stage_status.tsv')]]
 p['steps']=[{'step_id':str(i),'description':label,'outputs':outputs,'dependencies':deps} for i,label,outputs,deps in [
  (1,'Samples',['samples_tsv'],[]),(2,'Resolution',[],['1']),(3,'Balance',['matrix_balance_report'],['1']),
  (4,'Usable gate',[],['2','3']),(5,'Scientific outputs',['tad_domains','tad_boundaries','tad_summary'],['4']),
  (6,'Report',['report'],['3','5']),(7,'Reproduction',['reproduce_script'],['5']),(8,'Status',['stage_status'],['6','7'])]]
 return p

def data8(f):
 samples=write_samples(f);directory=f.root/'40000';directory.mkdir()
 balance=f.root/'mcool_balance_preflight.tsv';balance.write_text('sample\tmatrix\tresolution_bp\tusable_for_3d_genome_analysis\n'+''.join(s+'\t'+str(f.root/(s+'.mcool'))+'\t40000\tyes\n' for s in ['leaf','bud']))
 stats=directory/'tad_domain_stats.tsv';stats.write_text('sample\tresolution_bp\tcount\nleaf\t40000\t1\nbud\t40000\t1\n')
 summary=directory/'tad_run_summary.tsv';summary.write_text('sample\tresolution_bp\tstatus\treturn_code\nleaf\t40000\tsuccess\t0\nbud\t40000\tsuccess\t0\n')
 files=[samples,balance,stats,summary]
 for sample in ['leaf','bud']:
  for kind in ['domains','boundaries']:
   p=directory/(sample+'_'+kind+'.bed');p.write_text('chr1\t0\t40000\n');files.append(p)
 obs={'ok':True,'resolution_bp':40000,'inputs':[{'resource_handle':s,'path':str(f.root/(s+'.mcool')),'present':True,'target_group':'/resolutions/40000'} for s in ['leaf','bud']]}
 return files,json.dumps(obs)+'\n\n'+summary.read_text(),obs






@pytest.mark.parametrize('branch',['success','failed','blocked','missing','unusable','wrong_resolution'])
def test_D05_D06_saved_report_status_and_gate_branches(branch):
 async def run():
  async with task_fixture('delivery-'+branch) as f:
   _,_,obs=data8(f)
   manifest={'samples':['leaf','bud'],'inputs':{s:str(f.root/(s+'.mcool')) for s in ['leaf','bud']},'resolution_bp':40000,'balance_table':'mcool_balance_preflight.tsv','run_summary':'40000/tad_run_summary.tsv','statistics':'40000/tad_domain_stats.tsv','accepted_facts':[{'kind':'resolution','samples':['leaf','bud'],'resolution_bp':40000,'present':True}],'launcher':'/registered/python -u','scripts':{'resolution':'/skill/check_resolution.py','balance':'/skill/check_balance.py','analysis':'/skill/call_tads.py'},'multiple_testing':'fdr'}
   p=f.root/'40000/tad_run_summary.tsv';balance=f.root/'mcool_balance_preflight.tsv'
   if branch=='failed':p.write_text(p.read_text().replace('bud\t40000\tsuccess\t0','bud\t40000\tfailed\t1'))
   if branch=='missing':p.unlink()
   if branch=='unusable':balance.write_text(balance.read_text().replace('\tyes','\tno'))
   if branch=='wrong_resolution':balance.write_text(balance.read_text().replace('40000','10000'))
   result=render_delivery(f.root,manifest,execution_status='blocked' if branch=='blocked' else 'observed')
   expected={'success':'success','failed':'failed','blocked':'blocked','missing':'pending','unusable':'blocked','wrong_resolution':'blocked'}[branch]
   assert result['computation_status']==expected,result
   assert result['scientific_execution_count']==0
   text=(f.root/'report.md').read_text();assert 'does not certify or write framework acceptance' in text
   if branch in {'unusable','wrong_resolution'}:
    with pytest.raises(ValueError):usable_gate(f.root,manifest)
    manifest_path=f.root/'manifest.json';manifest_path.write_text(json.dumps(manifest))
    assert main(['gate','--root',str(f.root),'--manifest',str(manifest_path)])==2
   script=reproduction_script(manifest)
   assert script.index('"$DELIVERY_HELPER" gate') < script.index('call_tads.py') < script.rindex('"$DELIVERY_HELPER" render')
   assert 'set -euo pipefail' in script and 'rm ' not in script
 asyncio.run(run())




