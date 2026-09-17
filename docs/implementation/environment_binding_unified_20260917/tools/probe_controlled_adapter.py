import sys,json,hashlib,asyncio,yaml
from pathlib import Path
from types import SimpleNamespace
root=Path('/public/home/liusiqi/03.毕设/Biomind_v1')
sys.path.insert(0,str(root))
from agent_core.runtime_inspection import RuntimeInspectionAdapter
from agent_core.runtime_evidence import digest
from agent_core.runtime_probe_contract import probe_method
async def main():
 results={}
 for name,environment in [('wgs-short-read-mapping','bio_wgs'),('wgs-gatk-snp-indel','bio_gatk')]:
  document=(root/'skills/genomics'/name/'SKILL.md').read_text();meta=yaml.safe_load(document.split('---',2)[1]);req={**meta['runtime'],'env':environment};sid='genomics/'+name
  registry={row['capability_id']:row for row in meta['generate']['execution_capabilities']}
  subject={'skill_id':sid,'skill_document_digest':digest(document),'skill_contract_digest':digest(registry),'environment_id':environment,'environment_fingerprint':digest(str(Path('/public/home/liusiqi/anaconda3/envs')/environment)),'host_id':'local-read-only-observation','inspection_contract_digest':digest(probe_method(req))}
  manager=SimpleNamespace(skills_root=root/'skills',get_runtime_requirements=lambda _:req,get_skill_meta=lambda _:meta,get_execution_capability_registry=lambda _:registry)
  adapter=RuntimeInspectionAdapter(SimpleNamespace(conda_root='/public/home/liusiqi/anaconda3'),manager,scope_provider=lambda _:subject,path_validator=lambda output:dict(line.split('=',1) for line in output.splitlines() if '=' in line))
  contract=adapter.describe(sid)
  request={'scope':{'selected_skill_id':sid,'subject':subject,'required_binding_ids':contract['coverage_binding_ids']},'inspection_contract':contract,'operation':'inspect','inspection_receipt_id':'standalone-readonly-validation','inspection_deadline_seconds':60}
  result=await adapter.inspect(request)
  results[name]={'skill_sha256':hashlib.sha256(document.encode()).hexdigest(),'description':contract,'result':result}
 (root/'docs/implementation/environment_binding_unified_20260917/evidence/real_controlled_adapter.json').write_text(json.dumps(results,indent=2))
 print(json.dumps(results,indent=2))
asyncio.run(main())
