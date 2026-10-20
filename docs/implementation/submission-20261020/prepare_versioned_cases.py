from pathlib import Path
import json,shutil,hashlib
B=Path('/public/home/liusiqi/03.毕设'); R=B/'BioMind_Original_Cases_Revision_20261020'; O=B/'BioMind_Production_20261020/versioned-cases'; O.mkdir(exist_ok=True)
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def copy(p,q):q.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,q)
assets=[]
for p in (R/'inputs').rglob('*'):
 if not p.is_file():continue
 rel=p.relative_to(R)
 if p.is_symlink():assets.append({'path':str(rel),'workspace_path':str(p.resolve().relative_to(B)),'bytes':p.stat().st_size,'sha256':sha(p)})
 else:copy(p,O/rel)
(O/'external-assets.json').write_text(json.dumps(assets,ensure_ascii=False,indent=2)+'\n')
for p in (R/'tools').glob('*'):
 if p.is_file() and p.suffix in ['.py','.R','.sh']:copy(p,O/'tools'/p.name)
for p in (R/'review').glob('*'):
 if p.is_file():copy(p,O/'review'/p.name)
for name in ['manifest.json','交付与验收说明.txt']:copy(R/name,O/name)
for num,t in enumerate(json.loads((R/'manifest.json').read_text())['tasks'],1):
 P=Path(t['project_dir']);dest=O/'cases'/f'{num:02}'
 for name in [t['case_id']+'.md','input_manifest.json','data_inventory.tsv','input_status.tsv']:copy(P/name,dest/name)
 for p in (P/'analysis_support').glob('*.py'):copy(p,dest/'analysis_support'/p.name)
for name in ['final-audit.json','pav-recovery.json','sv-source-match.json','full-vcf-integrity.json','input-file-hashes.json']:
 copy(R/'evidence'/name,O/'validation'/name)
for model in ['gp_waiter','cropformer','dnngp']:
 for name in ['verification.json','fold_audit.json']:copy(R/'evidence/native-cv'/model/name,O/'validation/native-cv'/model/name)
for rel in ['gxe-verified/status.tsv','pangenome-native/verification.tsv','gwas-native/verification.json','gwas-native/downstream_verification.json','database-live/verification.json']:
 copy(R/'evidence'/rel,O/'validation'/rel)
(O/'README.txt').write_text('''四道原题输入修订，20261020-fullanalysis。

cases/ 保存已实际生效的四题题面、输入清单和可配置训练入口。inputs/ 保存本次生成的输入、映射和来源说明；external-assets.json 记录原始共享大文件的工作区相对位置、长度和 SHA256。它们留在原位，未重新提交或修改。运行 python tools/hydrate_external_assets.py --workspace <原工作区> 可核对并建立本地链接。
manifest.json 和题面仍使用该集群的真实绝对路径，是部署快照，不声称任意机器开箱即用。原始数据、旧题面备份、运行结果及评分均保留在原工作区；validation/ 仅保存本轮验收摘要，不含历史评分或模型权重。完整原始来源、训练检查点和日志位于 BioMind_Original_Cases_Revision_20261020。

本目录不属于 BioMind 核心运行时构建清单。生产 CLI 发布身份及本轮部署结果见 docs/implementation/submission-20261020/。
''')
print(json.dumps({'files':sum(p.is_file() for p in O.rglob('*')),'external_assets':len(assets),'directory':str(O)},ensure_ascii=False))
