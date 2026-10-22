"""Collect only fixed-schema startup telemetry, never raw backend logs."""
from pathlib import Path
import json
from deploy_studio import O,E,CONFIG
c=json.loads(CONFIG.read_text());state=Path(c['stateRoot']);d=json.loads((state/'service.json').read_text())
f=state/'logs'/(d['instanceId']+'.backend.log')
prefix='[biomind-startup-summary-v1] '
groups={};fields={'v','attemptId','role','pid','ppid','elapsedMs','stage','event','build','sessionId','ownerGeneration','exitCode','signal','code','childPid'}
with f.open() as stream:
    for line in stream:
        if not line.startswith(prefix):continue
        x=json.loads(line[len(prefix):]);a=x['attemptId'];g=groups.setdefault(a,{'attemptId':a,'records':[]})
        g['records'].extend({k:v for k,v in row.items() if k in fields} for row in x['records'])
        g['dropped']=x['dropped'];g['lastStage']=x['lastStage']
for g in groups.values():
    stages={};rows=g['records']
    for row in rows:
        if row['role']=='worker':
            stages.setdefault(row['stage'],{})[row['event']]=row['elapsedMs']
    g['phaseMs']={k:round(v['end']-v['begin'],3) for k,v in stages.items() if 'begin' in v and 'end' in v}
    if 'end' in stages.get('main',{}) and 'begin' in stages.get('node_start',{}):g['phaseMs']['preMain']=round(stages['main']['end']-stages['node_start']['begin'],3)
out={'instanceId':d['instanceId'],'attempts':list(groups.values()),'scope':'Production startup only; server/relay/worker monotonic origins differ; no model request'}
(E/'startup-diagnostics.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'attempts':[{'attemptId':g['attemptId'],'phaseMs':g['phaseMs'],'lastStage':g['lastStage']} for g in groups.values()]},ensure_ascii=False))
