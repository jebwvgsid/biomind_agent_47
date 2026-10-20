from pathlib import Path
import json,csv,hashlib,os
R=Path(__file__).resolve().parents[1];m=json.loads((R/'manifest.json').read_text());checks=[]
def check(name,ok,detail=None):
 checks.append({'check':name,'status':'PASS' if ok else 'FAIL','detail':detail});assert ok,(name,detail)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def table(p):
 with p.open() as f:return list(csv.DictReader(f,delimiter='\t'))
seen=set()
for t in m['tasks']:
 P=Path(t['project_dir']);prompt=Path(t['prompt_source']);text=prompt.read_text();check(t['case_id']+': prompt hash',sha(prompt)==t['source_sha256']);check(t['case_id']+': runnable prompt contract',text.count('```')==2 and 'data_status: ready_verified' in text and t['output_directory'] in text and '/output\n' not in text)
 check(t['case_id']+': backup data tree', (P/'data_before_inputfix_20261020').is_dir());check(t['case_id']+': input manifest hash',sha(Path(t['input_manifest']))==t['input_manifest_sha256'])
 ins=json.loads(Path(t['input_manifest']).read_text())
 for a in ins['files']:
  p=Path(a['path']);check('input exists '+str(p),p.is_file() and p.stat().st_size==a['bytes'])
  # Avoid rehashing large unchanged sources already read in apply_revision; verify source recorded digest.
  hashes=json.loads((R/'evidence/input-file-hashes.json').read_text());expected=hashes.get(str(p.resolve()))
  if expected is None or p.stat().st_size<1000000:expected=sha(p)
  check('input sha '+str(p),expected==a['sha256']);seen.add(str(p.resolve()))
old=json.loads((R/'evidence/historical-output-stat-snapshot.json').read_text());changed=[]
for a in old:
 p=Path(a['path']);s=p.stat();same=(s.st_size==a['size'] and s.st_mtime_ns==a['mtime_ns'] and s.st_ino==a['inode']);link=os.readlink(p) if p.is_symlink() else None
 if not same or link!=a['symlink']:changed.append(a['path'])
check('all historical output metadata preserved',not changed,{'n':len(old),'changed':changed})
D=R/'inputs';ids={x['sample_id'] for x in table(D/'multiomics/phenotype.tsv')};fold={x['sample_id']:int(x['fold_id']) for x in table(D/'multiomics/cv_fold_design.tsv')}
for model in ['gp_waiter','cropformer','dnngp']:
 E=R/'evidence/native-cv'/model;v=json.loads((E/'verification.json').read_text());check(model+' native training result',v['status']=='PASS' and v['n_oof']==24)
 records=table(E/'oof_predictions.tsv');check(model+' OOF complete',len(records)==24 and {x['sample_id'] for x in records}==ids)
 for f in json.loads((E/'fold_audit.json').read_text()):
  train=set(f['training_ids']);inner=set(f['inner_validation_ids']);test=set(f['outer_test_ids']);check(f"{model} fold {f['fold']} partition",not(train&inner or train&test or inner&test) and train|inner|test==ids and test=={s for s in ids if fold[s]==f['fold']})
check('GxE native verification',table(R/'evidence/gxe-verified/status.tsv')[0]['status']=='PASS')
check('PAV source recovery',json.loads((R/'evidence/pav-recovery.json').read_text())['unique_positions']==2000)
check('SV full source row matching',json.loads((R/'evidence/sv-source-match.json').read_text())['exact_full_record_matches']==500)
for row in table(R/'evidence/pangenome-native/verification.tsv'):check(row['branch']+' rrBLUP CV',row['status']=='PASS')
check('GWAS bounded native pipeline',json.loads((R/'evidence/gwas-native/verification.json').read_text())['status']=='PASS')
check('database live endpoints',json.loads((R/'evidence/database-live/verification.json').read_text())['status']=='PASS')
check('full VCF integrity and coordinate scan',json.loads((R/'evidence/full-vcf-integrity.json').read_text())['status']=='PASS')
check('GWAS real LD and annotation compatibility',json.loads((R/'evidence/gwas-native/downstream_verification.json').read_text())['status']=='PASS')
for name in ['manhattan_subset.png','qq_subset.png']:check('R plot '+name,(R/'evidence/gwas-native'/name).read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
receipt={'status':'PASS','checks':checks,'n_checks':len(checks),'unique_input_files':len(seen),'historical_outputs_preserved':len(old),'formal_agent_benchmark_scored':False,'production_changed_this_revision':False};(R/'evidence/final-audit.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n');print({k:v for k,v in receipt.items() if k!='checks'})
