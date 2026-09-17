"""Read-only actual SkillManager catalog + per-file policy evidence; no config import."""
from pathlib import Path
import hashlib,json,re,sys,datetime
ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT))
sys.dont_write_bytecode = True
from agent_core.skill_manager import SkillManager
OUT=Path(__file__).parent
HIST=Path('/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497/context_trace')
sm=SkillManager(str(ROOT/'skills'))
pattern=re.compile(r'(?i)install|安装|配置环境|no.?mutation')
requests=[]; extracts=[]
for p in sorted(HIST.glob('*/request.json')):
 data=json.loads(p.read_text())
 for i,m in enumerate(data.get('messages',[])):
  content=m.get('content','')
  if not isinstance(content,str):continue
  requests.append((p,i,content))
  for j,l in enumerate(content.splitlines(),1):
   if pattern.search(l) and not ('<skill_catalog>' in l):
    if len(l)>1200: continue
    extracts.append({'request':str(p),'json_pointer':f'/messages/{i}/content','content_line':j,'quote':l})
rows=[]
# Human-reviewed semantic groups, not keyword-count inference.
delegation={'3d-dna/differential-tad','3d-dna/tad-boundary-change','3d-dna/tad-calling','epigenomics/methylation/gene-plot','genomics/assembly','scrna/env/bootstrap','scrna/workflows/ingest-qc','scrna/annotate/basic','scrna/cluster/basic','scrna/de/contrast','scrna/ingest/data-download','scrna/markers/find','scrna/preprocess/basic','scrna/preprocess/sctransform','scrna/qc/cell-cycle-doublet','scrna/qc/filter'}
ambiguous={'genomics/wgs-short-read-mapping':'范围冲突：正文禁安装/缺能力 block，与 prefer-declared 元数据及安装工单混用；未明说禁止询问授权', 'genomics/wgs-cnvnator-cnv':'排错表建议 activate/install；未明确委派角色', 'genomics/wgs-manta-sv':'排错表建议 install/register；未明确委派角色','open-chromatin/modkit-cpu':'排错表建议 Install/activate bedtools；未明确委派角色'}
for s in sm.discovered_skills:
 sid=s['_skill_id']; p=Path(s['_skill_md']); raw=p.read_text(); rt=s.get('runtime') or {}
 related=[]; checks=[]
 for f in sorted(p.parent.rglob('*')):
  if not f.is_file() or f.suffix.lower() in {'.pyc','.pyo'} or '__pycache__' in f.parts:continue
  if any(part.startswith('.') for part in f.relative_to(p.parent).parts):continue
  if f.stat().st_size>400000:continue
  try:text=f.read_text()
  except UnicodeError:continue
  lines=text.splitlines(); hits=[]
  for i,l in enumerate(lines,1):
   if pattern.search(l): hits.append({'line':i,'quote':l,'context':'\n'.join(lines[max(0,i-2):min(len(lines),i+2)])})
  checks.append({'path':str(f),'sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'line_count':len(lines)})
  if hits:related.append({'path':str(f),'rules_or_mentions':hits,'delivery':'正文/元数据由 loader 投影；其他文件只列路径，须实际读取/脚本执行才可见'})
 if sid in ambiguous: policy=ambiguous[sid]; category='C 范围不清/冲突'
 elif sid in delegation:policy='科学步骤不得直接安装；缺失后委派安装并重新验证';category='D 明确委派'
 elif rt.get('install_policy')=='declared-only':policy='仅允许声明的安装命令/回退；目标与工具有独立限制';category='L 限定安装方案'
 elif sid.endswith('/snpeff-annotation'):policy='healthcheck失败时仅在许可下使用声明安装器';category='A 条件许可'
 elif (p.parent/'references/install-and-data.md').exists():policy='安装/复现需求时先读安装SOP；含环境创建/下载命令，角色授权须另行约束';category='S 条件安装SOP'
 elif rt.get('install_command') or rt.get('install_commands') or rt.get('install_tool'):policy='声明安装命令或委派token；正文未进一步限定授权/角色';category='M 安装元数据'
 elif sid=='databases/ncbi/entrez-fetch':policy='正文未声明；可选 usage-guide 含 pip install 示例，未标注委派';category='S 参考安装示例'
 elif sid in {'crop-breeding/crop-gs-bglr-bayesian','crop-breeding/crop-rnaseq-de'}:policy='正文未声明；脚本仅报缺包并附安装建议，不自行安装';category='E 缺依赖报错'
 else:policy='未声明安装政策；版本描述/依赖错误/生物学environment不视为安装授权';category='N 不涉及安装政策'
 loc=[]
 for req,i,c in requests:
  if f'<loaded_skill_doc skill_id="{sid}">' in c or ('---BEGIN EXACT SOURCE BODY---' in c and f'"skill_id":"{sid}"' in c and '"source_kind":"skill_document"' in c):
   loc.append({'request':str(req),'json_pointer':f'/messages/{i}/content','evidence_kind':'loaded_skill_doc or inherited_skill_material exact source in request; individual rule quote matched separately'})
 rows.append({'skill_id':sid,'path':str(p),'sha256':s['_skill_content_hash'],'declared_version':s.get('version',''),'category':category,'policy':policy,'runtime_install_policy':rt.get('install_policy',''),'runtime_default_env':rt.get('default_env',''),'references':s.get('_references',[]),'files_reviewed':checks,'related_rule_evidence':related,'historical_document_locations':loc,'stage_inheritance':'No filesystem parent-template auto inheritance; selected/requested exact docs only. Installer currently embeds clipped selected scientific doc in work order.'})
all_paths=sorted(ROOT.rglob('SKILL.md'))
active={r['path'] for r in rows}
excluded=[str(p) for p in all_paths if str(p.resolve()) not in active]
assert len(rows)==88
assert not any(k=='config' or k.startswith('config.') for k in sys.modules)
report={'audit_time':datetime.datetime.now(datetime.timezone.utc).isoformat(),'loader':str(ROOT/'agent_core/skill_manager.py'),'loader_sha256':hashlib.sha256((ROOT/'agent_core/skill_manager.py').read_bytes()).hexdigest(),'catalog':sm.catalog_status(),'root':str(sm.skills_root),'runtime_registry':str(sm.runtime_registry_path),'runtime_registry_sha256':hashlib.sha256(sm.runtime_registry_path.read_bytes()).hexdigest(),'ambiguous_aliases':sorted(sm._ambiguous_skill_keys),'all_active_skills':rows,'excluded_repo_SKILL_paths':excluded,'config_imported':False}
(OUT/'skills_inventory.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
(OUT/'skills_historical_rule_quotes.json').write_text(json.dumps(extracts,ensure_ascii=False,indent=2)+'\n')
# Each row has its own exact path, version and applicable rules; evidence JSON preserves all snippets/consumers.
md=['# 全体现役 Skills 安装政策矩阵','',f'实际 SkillManager 加载根：`{sm.skills_root}`；{len(rows)} 项，完整目录，revision `{sm.catalog_status()["catalog_revision"]}`。','', '列义：D=科学执行禁直接安装但允许委派；L=安装方案严格限定；A=条件许可；S=安装SOP/例子；M=仅声明元数据；C=作用域冲突/不清；E=脚本报错建议；N=不涉及安装政策。没有因为关键词未命中而判定“允许任意安装”。公共规则、角色继承与历史差异见 install_skills.md。安装语句的全部局部上下文与相关文件哈希见 skills_inventory.json；这里只压缩展示。', '', '| 现役 Skill / 源码 | 类别与语义 | 相关定位（行号） | 本次实际正文可见 |', '|---|---|---|---|']
for row in rows:
 refs=[]
 for f in row['related_rule_evidence']:
  rel=str(Path(f['path']).relative_to(ROOT)); hits=f['rules_or_mentions']
  if hits:refs.append(rel+':'+','.join(str(h['line']) for h in hits))
 h=sorted({Path(x['request']).parent.name.split('-')[0] for x in row['historical_document_locations']})
 md.append(f'| [{row["skill_id"]}]({row["path"]}) | {row["category"]}：{row["policy"]} | '+ ('<br>'.join(refs) if refs else '全文已读；无安装约束条款')+' | '+('/'.join(h) if h else '本次未载入正文')+' |')
(OUT/'skills_policy_matrix.md').write_text('\n'.join(md)+'\n')
print(json.dumps({'count':len(rows),'catalog':sm.catalog_status(),'excluded_count':len(excluded),'historical_rule_quotes':len(extracts),'config_imported':False},ensure_ascii=False))
