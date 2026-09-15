"""Collect compact offline receipts. Reads only the explicitly supplied test root."""
from pathlib import Path
import gzip
import hashlib
import json
import shutil
import sys

source=Path(sys.argv[1]).resolve()
destination=Path(sys.argv[2]).resolve()
sources=[source,*[Path(p).resolve() for p in sys.argv[3:]]]
destination.mkdir(parents=True,exist_ok=True)

def sha(value):return hashlib.sha256(value).hexdigest()
def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def write(name,value):
    (destination/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

names={'phase_b_review_commit.json','layout_contract_receipt.json','causal_projection_sizes.json',
    'targeted_large_maps.json','four_episode_wire_composition.json','saved_prefix_boundaries.json'}
manifest=[];controls=[];approvals=[];cancellations=[];integrations=[];facts=[]
for path in [p for root in sources for p in sorted(root.rglob('*.json'))]:
    name=path.name
    if name not in names and name not in {'control_receipt.json','approval_exit_receipt.json',
        'cancellation_receipt.json','phase_b_compaction_integration.json'} and not name.startswith('fact_projection_'):
        continue
    value=json.loads(path.read_text())
    manifest.append({'path':str(path),'sha256':sha(path.read_bytes())})
    if name in names:write(name,value)
    elif name.startswith('fact_projection_'):facts.append(value)
    elif name=='control_receipt.json':controls.append(value)
    elif name=='approval_exit_receipt.json':approvals.append(value)
    elif name=='cancellation_receipt.json':cancellations.append(value)
    else:
        activation=value['activation'];archives=[]
        # Non-applied cases may still have an immutable candidate or an admitted
        # Recovery receipt. Do not turn a missing durable ledger into zero usage.
        ledgers=[]
        def maintenance_rows(v):
            if isinstance(v,dict):
                if isinstance(v.get('compaction_maintenance'),dict):yield v['compaction_maintenance']
                for child in v.values():yield from maintenance_rows(child)
            elif isinstance(v,list):
                for child in v:yield from maintenance_rows(child)
        for stored in list((path.parent/'phase_archive/objects').rglob('*.json'))+list((path.parent/'task_outputs').glob('*/plan_state.json')):
            ledgers.extend(maintenance_rows(json.loads(stored.read_text())))
        if ledgers:
            held_ledger=max(ledgers,key=lambda r:len(r.get('calls',[])))
            value['maintenance_ledger']=held_ledger.get('calls',[])
            value['maintenance_charged_tokens']=held_ledger.get('charged_tokens')
            value['maintenance_elapsed_seconds']=held_ledger.get('elapsed_seconds')
        if activation:
            for label in ('source_archive_ref','candidate_epoch_archive_ref'):
                digest=activation[label].split(':')[-1]
                archive=path.parent/'phase_archive/objects'/digest[:2]/(digest+'.json')
                held=json.loads(archive.read_text())
                assert held['archive_ref']==activation[label] and held['digest']==digest
                assert sha(canonical({k:v for k,v in held.items() if k not in {'digest','archive_ref'}}))==digest
                target=value['mode']+'_'+label+'.json.gz'
                (destination/target).write_bytes(gzip.compress(archive.read_bytes(),mtime=0))
                archives.append({'label':label,'archive_ref':held['archive_ref'],'artifact':target,
                    'original_file_sha256':sha(archive.read_bytes()),'content_digest_verified':True})
                if label=='source_archive_ref':
                    value['same_sample_before_compaction_chars']=sum(len(str(m.get('content',''))) for m in held['transcript'])
                else:
                    maintenance=held['summary'].get('business_resume_state',{}).get('compaction_maintenance',{})
                    value['maintenance_ledger']=maintenance.get('calls',[])
                    value['maintenance_charged_tokens']=maintenance.get('charged_tokens')
                    value['maintenance_elapsed_seconds']=maintenance.get('elapsed_seconds')
            next_wire=path.parent/'actual_next_request.json.gz'
            target=value['mode']+'_actual_next_request.json.gz'
            shutil.copyfile(next_wire,destination/target)
            messages=json.loads(gzip.decompress(next_wire.read_bytes()))
            assert sha(json.dumps(messages,sort_keys=True,ensure_ascii=False).encode())==value['wire_components'][-1]['wire_value_sha256']
            assert value['wire_components'][-1]['summary_present'] and not value['wire_components'][-1]['old_large_body_present']
            value['next_request_artifact']=target
        value['archive_receipts']=archives
        integrations.append(value)
integrations=list({r['mode']:r for r in integrations}.values())
write('fact_projections.json',facts)
write('control_commands.json',{'interactive_subprocesses':controls,'approval_boundaries':approvals,'cancellation_views':cancellations})
write('integrated_replay.json',integrations)
write('source_receipt_manifest.json',manifest)
write('metrics.json',{'method':'isolated production owners with deterministic provider fixtures; character counts are not provider usage',
    'cases':[{'mode':r['mode'],'business_calls':r['business_calls'],'maintenance_calls':r['maintenance_calls'],
        'maps':sum(c['operation']=='map_source_segment' for c in r['compactor_calls']),
        'final_plans':sum(c['operation']=='final' for c in r['compactor_calls']),
        'applied':bool(r['activation']),'same_sample_before_compaction_chars':r.get('same_sample_before_compaction_chars'),
        'business_wire_content_chars':r['wire_chars'],'charged_or_reserved_tokens':r.get('maintenance_charged_tokens'),
        'known_provider_usage_requests':0,
        'unknown_usage_requests':r['maintenance_calls'],
        'usage_basis':'deterministic provider fixture supplies no usage; not real billed requests',
        'durably_captured_maintenance_calls':len(r.get('maintenance_ledger',[])),
        'total_maintenance_wire_content_chars':sum(c['wire_chars'] for c in r['compactor_calls'])} for r in integrations],
    'live_model':'NOT_RUN','real_cache_rate':'NOT_RUN','science_e2e':'NOT_RUN',
    'historical_unknown_usage_not_replaced':True})
write('artifact_hashes.json',{str(p.relative_to(destination)):sha(p.read_bytes()) for p in sorted(destination.iterdir()) if p.is_file() and p.name!='artifact_hashes.json'})
print(json.dumps({'integrations':len(integrations),'interactive_subprocesses':len(controls),'approval_boundaries':len(approvals),'fact_projections':len(facts)}))
