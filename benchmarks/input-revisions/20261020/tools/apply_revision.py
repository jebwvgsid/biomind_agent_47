"""Apply the user-authorized revision to the original four case directories.
Never touch historical outputs/scores or production software.
"""
from pathlib import Path
import json,hashlib,csv,shutil,difflib,os
R=Path(__file__).resolve().parents[1];cases=json.loads((R/'evidence/original-cases.json').read_text());groups={1:['gwas'],2:['multiomics'],3:['gxe','pangenome'],4:['database']};cache={};task=[];(R/'review').mkdir(exist_ok=True)
def sha(p):
 p=p.resolve()
 if str(p) not in cache:
  h=hashlib.sha256()
  with p.open('rb') as f:
   for b in iter(lambda:f.read(1048576),b''):h.update(b)
  cache[str(p)]=h.hexdigest()
 return cache[str(p)]
for c in cases:
 i=c['index'];P=Path(c['project']);stage=R/'staged'/f'{i:02d}';data=stage/'data';data.mkdir(exist_ok=True)
 for g in groups[i]:
  link=data/g
  if not link.exists():link.symlink_to(R/'inputs'/g,target_is_directory=True)
 # Stage immutable active script copy, not a link to a mutable validation log.
 if i==2:
  support=stage/'analysis_support';support.mkdir(exist_ok=True);shutil.copy2(R/'tools/train_multiomics.py',support/'train_multiomics.py')
 prompt=stage/(c['case_id']+'.md');txt=prompt.read_text().replace('ambiguity_level: strict\n','ambiguity_level: strict\ndata_status: ready_verified\n');prompt.write_text(txt)
 files=[]
 for g in groups[i]:
  for p in sorted((R/'inputs'/g).rglob('*')):
   if p.is_file():
    rel=Path('data')/g/p.relative_to(R/'inputs'/g);files.append({'relative_path':str(rel),'path':str(P/rel),'resolved_source':str(p.resolve()),'bytes':p.stat().st_size,'sha256':sha(p),'role':'formal_input_or_source_document'})
 if i==2:
  p=stage/'analysis_support/train_multiomics.py';files.append({'relative_path':'analysis_support/train_multiomics.py','path':str(P/'analysis_support/train_multiomics.py'),'resolved_source':str(P/'analysis_support/train_multiomics.py'),'bytes':p.stat().st_size,'sha256':sha(p),'role':'native_model_entrypoint'})
 m={'case_id':c['case_id'],'input_revision':'20261020-fullanalysis','purpose':'actual_analysis','original_question_backed_up_at':c['backup'],'output_directory':str(P/'output_inputfix_20261020'),'files':files,'hidden_truth_excluded':True,'historical_scores_reused':False}
 (stage/'input_manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
 with (stage/'data_inventory.tsv').open('w',newline='') as f:
  w=csv.DictWriter(f,['relative_path','bytes','sha256','role'],delimiter='\t',extrasaction='ignore');w.writeheader();w.writerows(files)
 with (stage/'input_status.tsv').open('w',newline='') as f:
  w=csv.writer(f,delimiter='\t');w.writerow(['case_id','input_revision','input_status','analysis_requirement','verification_scope']);w.writerow([c['case_id'],'20261020-fullanalysis','ready_verified','all_stages_must_run',{1:'real 5000-record subset through PLINK/K/EMMAX; full VCF not rerun',2:'all 3 native models, 5 folds, 24 OOF each',3:'GxE 2 validation targets; 380 epav +28 SV phenotype pairing; native rrBLUP CV',4:'live GEO search/summary, SRA+PubMed link/fetch, Ensembl'}[i]])
 old=Path(c['backup'])/(c['case_id']+'.md');(R/'review'/(c['case_id']+'.patch')).write_text(''.join(difflib.unified_diff(old.read_text().splitlines(True),prompt.read_text().splitlines(True),fromfile=str(old),tofile=str(P/prompt.name))))
 # Idempotence only for our own revision; preserve old data tree intact before replacement.
 dest=P/'data';historical=P/'data_before_inputfix_20261020'
 if not historical.exists():
  if dest.exists() or dest.is_symlink():dest.rename(historical)
 if not dest.exists():shutil.copytree(data,dest,symlinks=True)
 else:
  for g in groups[i]:assert (dest/g).is_symlink() and (dest/g).resolve()==(R/'inputs'/g).resolve()
 for name in [prompt.name,'input_manifest.json','data_inventory.tsv','input_status.tsv']:
  temp=P/(name+'.new-inputfix');shutil.copy2(stage/name,temp);temp.replace(P/name)
 if i==2:
  sd=P/'analysis_support';sd.mkdir(exist_ok=True);target=sd/'train_multiomics.py';assert not target.exists() or sha(target)==sha(stage/'analysis_support/train_multiomics.py');shutil.copy2(stage/'analysis_support/train_multiomics.py',target)
 task.append({'case_id':c['case_id'],'project_dir':str(P),'prompt_source':str(P/prompt.name),'source_sha256':sha(P/prompt.name),'output_directory':m['output_directory'],'input_manifest':str(P/'input_manifest.json'),'input_manifest_sha256':sha(P/'input_manifest.json'),'input_revision':m['input_revision'],'status':'INPUT_REVISED_NATIVE_VALIDATION','formal_benchmark_scored':False})
 print('APPLIED',c['case_id'],len(files),'input files',flush=True)
manifest={'version':'20261020-fullanalysis','description':'Original four case directories, revised complete-analysis input contract; old results/scores preserved. This is a task/input manifest, not a claim of runner schema compatibility.','tasks':task,'production_deployed':False,'formal_agent_benchmark_runs_started':False,'historical_scores_reused':False}
(R/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');(R/'evidence/input-file-hashes.json').write_text(json.dumps(cache,ensure_ascii=False,indent=2)+'\n')
# GP entrypoint diff against the original hard-coded training driver, for review only.
original=R.parent/'benchmarks/runtime_setup_20261014/output/sources/GP-WAITER/train-test.py';adapted=Path(cases[1]['project'])/'analysis_support/train_multiomics.py'
(R/'review/gp_waiter_training_entrypoint.patch').write_text(''.join(difflib.unified_diff(original.read_text().splitlines(True),adapted.read_text().splitlines(True),fromfile=str(original),tofile=str(adapted))))
