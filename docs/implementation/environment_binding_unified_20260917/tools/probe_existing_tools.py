import sys, json, yaml, hashlib, os, tempfile
from pathlib import Path
root=Path('/public/home/liusiqi/03.毕设/Biomind_v1')
sys.path.insert(0,str(root))
from agent_core.runtime_probe_contract import inspect_declared_runtime,build_probe_specification
results={}
for name,environment in [('wgs-short-read-mapping','bio_wgs'),('wgs-gatk-snp-indel','bio_gatk')]:
 text=(root/'skills/genomics'/name/'SKILL.md').read_text()
 meta=yaml.safe_load(text.split('---',2)[1])
 result=inspect_declared_runtime(meta['runtime'],Path('/public/home/liusiqi/anaconda3/envs')/environment)
 results[name]={'skill_sha256':hashlib.sha256(text.encode()).hexdigest(),'environment':environment,**result}
output=root/'docs/implementation/environment_binding_unified_20260917/evidence/real_runtime_probes.json';output.write_text(json.dumps(results,indent=2))
print(json.dumps(results,indent=2))
