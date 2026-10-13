#!/usr/bin/env python3
"""Bind compiled Next snapshots/UI protocol to executed compatibility evidence."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re

ADAPTER_CONTRACT=('canonicalRequestBeforeState','canonicalResolvedBeforeState','subscribeReplaysCompleteQuestions',
    'httpCompleteDisplayPendingUI','httpSequenceCoversEmittedState','liveToolSnapshot','noTaskOrAnswerSubmitted')
BRIDGE_CONTRACT=('actualCompiledAdapterToBridge','canonicalMetadataPreserved','rawMismatchRestoresCompleteHttpQuestions',
    'getInducedStateCoalesced','subsequentSameIdsDoNotQuery','canonicalResolutionApplied','noTaskOrAnswerSubmitted')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compatibility(backend,proof_path,build_id):
    result=dict(path=str(proof_path),verified=False)
    try:
        raw=proof_path.read_bytes()
        if len(raw)>1024*1024:raise ValueError('proof size')
        proof=json.loads(raw)
        result['sha256']=hashlib.sha256(raw).hexdigest()
        task=backend.parent.parent
        expected={'source':backend/'lib/biomind-session-adapter.ts',
                  'bridge':task/'desktop/electron/biomind-bridge.cjs',
                  'test':task/'scripts/test-compiled-adapter.cjs'}
        checks={
            'schema':proof.get('schema')=='BioMindCompiledAdapterCompatibilityV1',
            'result':proof.get('result')=='PASS',
            'buildId':proof.get('buildId')==build_id,
            'noModelOrNetwork':proof.get('modelStarted') is False and proof.get('networkStarted') is False,
            'compiledAdapterContract':all(proof.get('compiled',{}).get('contract',{}).get(key) is True for key in ADAPTER_CONTRACT),
            'sourceAdapterContract':all(proof.get('source',{}).get('contract',{}).get(key) is True for key in ADAPTER_CONTRACT),
            'compiledBridgeContract':all(proof.get('bridge',{}).get('contract',{}).get(key) is True for key in BRIDGE_CONTRACT),
        }
        for key,path in expected.items():
            record=proof.get(key,{})
            checks[key+'BytesMatch']=record.get('path')==str(path) and record.get('sha256')==digest(path)
        result.update(checks=checks,verified=all(checks.values()),compiled=proof.get('compiled'),
                      optionalSourceSseDisplayPendingUI=proof.get('source',{}).get('contract',{}).get('optionalSseDisplayPendingUI'))
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        result.update(code='COMPILED_COMPATIBILITY_PROOF_MISSING_OR_INVALID')
    return result


def check(backend,proof_path=None):
    backend=Path(backend).resolve()
    server=backend/'.next-pkg/server'
    build_id_path=backend/'.next-pkg/BUILD_ID'
    build_id=build_id_path.read_text().strip() if build_id_path.is_file() else None
    proof_path=Path(proof_path).resolve() if proof_path else backend.parent.parent/'evidence/compiled-adapter-compatibility.json'
    proof=compatibility(backend,proof_path,build_id)
    rows=[]
    required=('liveSnapshot(){','liveSnapshot=this.liveSnapshot','liveSnapshot:this.liveSnapshot()',
              'displayPendingUI:[...this.questions.values()]')
    labels=('liveSnapshotGetter','sseLiveSnapshot','httpLiveSnapshot','httpDisplayPendingUI')
    for filename in sorted((server/'chunks').glob('*.js')):
        if filename.stat().st_size>64*1024*1024:continue
        raw=filename.read_bytes()
        if b'liveSnapshot' not in raw:continue
        code=raw.decode('utf8');chunk_sha=hashlib.sha256(raw).hexdigest()
        checks=dict(zip(labels,(needle in code for needle in required)))
        chunk_id=filename.stem;route_refs={}
        for route in ('app/api/agent/[id]/route.js','app/api/agent/[id]/events/route.js'):
            path=server/route
            text=path.read_text() if path.is_file() else ''
            lists=re.findall(r'\.X\(0,\[([^\]]+)\]',text)
            route_refs[route]=any(chunk_id in [item.strip() for item in group.split(',')] for group in lists)
        compiled=proof.get('compiled') or {}
        proof_matches=proof['verified'] and compiled.get('chunk')==str(filename) and compiled.get('sha256')==chunk_sha
        rows.append(dict(chunk=str(filename),sha256=chunk_sha,checks=checks,
            optionalSseDisplayPendingUI='displayPendingUI=[...this.questions.values()]' in code,
            routesReferenceChunk=route_refs,executedCompatibilityProofMatches=proof_matches,
            passed=all(checks.values()) and all(route_refs.values()) and proof_matches))
    passed=bool(build_id) and any(row['passed'] for row in rows)
    return dict(schema='BioMindWorkbenchCompiledBackendCheckV2',checkedAt=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        backendRoot=str(backend),buildId=build_id,result='PASS' if passed else 'FAIL',compiledChunks=rows,
        compatibilityProof=proof,contract='canonical ordered UI events + authoritative HTTP displayPendingUI + live snapshots',
        modelStarted=False,windowsAcceptance='NOT_RUN',newSlurmBuildClaimed=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-root',required=True)
    parser.add_argument('--compatibility-proof')
    parser.add_argument('--output')
    args=parser.parse_args()
    try:
        receipt=check(args.backend_root,args.compatibility_proof)
    except (OSError,ValueError,UnicodeError):
        receipt=dict(schema='BioMindWorkbenchCompiledBackendCheckV2',result='FAIL',code='BUNDLE_INSPECTION_FAILED')
    if args.output:Path(args.output).write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(receipt,ensure_ascii=False))
    raise SystemExit(0 if receipt['result']=='PASS' else 2)
