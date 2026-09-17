"""Read-only current-state witnesses. No models, installation or real analysis.

Imports only real capability/resolver/runner modules; package __init__ and config
are isolated to prevent startup, registry repair or dotenv side effects.
"""
import asyncio
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types

REPO = Path(__file__).resolve().parents[4]
OUT = Path(__file__).with_name('runtime_witness_results.json')
sys.path.insert(0, str(REPO))
os.environ['BIOMIND_DISABLE_DOTENV'] = '1'
sys.dont_write_bytecode = True

def reject_sensitive_reads(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        name = os.fsdecode(args[0])
        if Path(name).name.startswith('.env'):
            raise PermissionError('Audit forbids dotenv access')
sys.addaudithook(reject_sensitive_reads)

# Bypass the execution package's unrelated optional dependency initializers.
package = types.ModuleType('execution')
package.__path__ = [str(REPO / 'execution')]
sys.modules['execution'] = package
from agent_core.action_runtime import (
    build_runtime_binding_registry, resolve_action_runtime_bindings,
    build_exact_action_environment, generic_execution_capability,
    validate_runtime_paths_prelaunch, ActionRuntimeContractError,
)
from agent_core.skill_manager import SkillManager
from execution.executor import run_command
from agent_core.runtime_inspection import RuntimeInspectionAdapter
from agent_core.runtime_evidence import SCOPE_FIELDS, digest

async def main():
    manager = SkillManager(str(REPO / 'skills'))
    sid = 'genomics/wgs-short-read-mapping'
    cid = 'genomics.wgs.short.read.mapping.prepare_reference'
    contracts = manager.get_execution_capability_registry([sid])
    prefix = '/public/home/liusiqi/anaconda3/envs/bio_wgs'
    audit = {'skill_id': sid, 'checked_env': 'bio_wgs', 'status': 'ready',
             'resolved_runtime_paths': {'RESOLVED_ENV_PREFIX': prefix,
                                        'RESOLVED_PICARD_BIN': prefix + '/bin/picard'}}
    registry = build_runtime_binding_registry([audit])
    def resolve(contract, facts):
        return resolve_action_runtime_bindings(selected_skill_id=contract.get('selected_skill_id', ''),
            execution_capability_id=contract['capability_id'], approved_capability_contract_hash='',
            capability_registry={contract['capability_id']: contract},
            ready_runtime_binding_registry=facts,
            current_action_identity={'action_id':'audit', 'plan_id':'audit', 'step_id':'fixture'})
    results = []
    resolution = resolve(contracts[cid], registry)
    assert resolution['runtime_binding_manifest']['bindings'] == []
    results.append({'id':'declared_environment_and_picard_do_not_enlarge_empty_capability',
        'status':'PASS_CURRENT_STATE_WITNESS', 'contract':contracts[cid],
        'provided_candidate_fact_ids':list(registry), 'result':resolution})
    generic = resolve(generic_execution_capability(), {})
    assert generic['runtime_binding_manifest']['bindings'] == []
    results.append({'id':'legitimate_generic_empty_binding', 'status':'PASS_CURRENT_STATE_WITNESS'})
    # A fixture contract exercises existing fail-closed behavior; no Skill edit.
    explicit = dict(contracts[cid], required_runtime_binding_ids=['runtime.picard.bin'])
    other = dict(audit, resolved_runtime_paths={'RESOLVED_PICARD_BIN':'/audit/other/picard'})
    ambiguous = build_runtime_binding_registry([audit, other])
    try:
        resolve(explicit, ambiguous)
        raise AssertionError('ambiguous accepted')
    except ActionRuntimeContractError as exc:
        assert exc.code == 'REQUIRED_RUNTIME_BINDING_UNAVAILABLE'
        results.append({'id':'conflicting_candidates', 'status':'PASS_CURRENT_STATE_WITNESS',
                        'diagnostic':exc.to_diagnostic()})
    invalid = build_runtime_binding_registry([other])
    try:
        validate_runtime_paths_prelaunch(resolve(explicit, invalid))
        raise AssertionError('invalid path accepted')
    except ActionRuntimeContractError as exc:
        assert exc.code == 'RUNTIME_BINDING_PRELAUNCH_PATH_INVALID'
        results.append({'id':'stale_registered_path', 'status':'PASS_CURRENT_STATE_WITNESS',
                        'diagnostic':exc.to_diagnostic()})
    # PackageManager initializer writes/repairs registries; bypass it and config.
    config = types.ModuleType('config')
    config.config = types.SimpleNamespace(BIO_SOFTWARE_ROOT='', BIO_SOURCE_INSTALL_DIR='', ANACONDA_ROOT='')
    sys.modules['config'] = config
    PM = importlib.import_module('package_manager').PackageManager
    pm = PM.__new__(PM)
    pm.conda_root = '/public/home/liusiqi/anaconda3'
    requirement = manager.get_runtime_requirements(sid)
    transformed, process_env = pm._direct_environment_healthcheck('bio_wgs', requirement['healthcheck'])
    assert process_env['PATH'].startswith(prefix + '/bin:')
    assert transformed.startswith('bash -c ')
    results.append({'id':'skill_healthcheck_wraps_and_pm_uses_declared_prefix',
                    'status':'PASS_CURRENT_STATE_WITNESS', 'declared_env':requirement['env'],
                    'healthcheck':requirement['healthcheck'], 'effective_command':transformed,
                    'subprocess_env_allowlist':{k:process_env.get(k) for k in ('PATH','CONDA_PREFIX','CONDA_DEFAULT_ENV','JAVA_HOME')}})
    subject={key:'sha256:'+'0'*64 for key in SCOPE_FIELDS}
    subject.update(skill_id=sid,environment_id='bio_wgs',
        inspection_contract_digest=digest(requirement['healthcheck']))
    inspection=RuntimeInspectionAdapter(pm,manager,scope_provider=lambda _:subject,
        path_validator=lambda _:{}).describe(sid)
    assert inspection['permitted'] is False
    assert inspection['reason_code'] == 'RUNTIME_INSPECTION_SYNTAX_UNSUPPORTED'
    results.append({'id':'existing_runtime_inspection_rejects_wgs_healthcheck_syntax',
        'status':'PASS_CURRENT_STATE_WITNESS','result':inspection,
        'note':'Existing adapter.describe only; never executes a probe.'})
    old = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix='biomind-runtime-witness-') as tmp:
        base = {'PATH':'/usr/bin:/bin', 'LC_ALL':'C', 'TMPDIR':tmp}
        env0 = build_exact_action_environment(base, action_environment={})
        stdout, stderr, rc = await run_command('command -v picard', timeout=5,
            profile=False, env=env0, max_output_bytes=4096, display_streams=False)
        assert rc != 0
        results.append({'id':'real_runner_empty_action_environment', 'status':'PASS_CURRENT_STATE_WITNESS',
                        'environment':env0,'command':'command -v picard','timeout_seconds':5,
                        'output_limit_bytes':4096,'returncode':rc,'stdout':stdout,'stderr':stderr})
        # Run the actual Skill->PM healthcheck text in a controlled command set.
        # BWA and BWA-MEM2 are absent; head succeeds without pipefail.
        fixture_bin=Path(tmp)/'healthcheck-bin'
        fixture_bin.mkdir()
        (fixture_bin/'bash').symlink_to('/bin/bash')
        (fixture_bin/'head').symlink_to('/usr/bin/head')
        for tool in ('samtools','picard'):
            f=fixture_bin/tool
            f.write_text('#!/bin/sh\nexit 0\n')
            f.chmod(0o700)
        checkenv={**base,'PATH':str(fixture_bin)}
        stdout,stderr,rc=await run_command(transformed,timeout=5,profile=False,
            env=checkenv,max_output_bytes=4096,display_streams=False)
        assert rc == 0
        results.append({'id':'coarse_healthcheck_accepts_missing_bwa_pipeline',
            'status':'PASS_CURRENT_STATE_WITNESS','command':transformed,
            'environment':checkenv,'tools_present':['bash','head','samtools','picard'],
            'tools_absent':['bwa','bwa-mem2'],'returncode':rc,'stdout':stdout,'stderr':stderr,
            'timeout_seconds':5,'output_limit_bytes':4096})
        # Synthetic commands exercise two independent action environments and Java.
        for tag in ('a','b'):
            folder = Path(tmp)/tag
            folder.mkdir()
            java = folder/'java'
            java.write_text('#!/bin/sh\nprintf "java-'+tag+'\\n"\n')
            java.chmod(0o700)
        for tag in ('a','b'):
            action_env = {'PATH':str(Path(tmp)/tag)+':/usr/bin:/bin','JAVA_HOME':str(Path(tmp)/tag)}
            effective = build_exact_action_environment(base, action_environment=action_env)
            stdout, stderr, rc = await run_command('java', timeout=5, profile=False,
                env=effective, max_output_bytes=4096, display_streams=False)
            assert rc == 0 and stdout.strip() == 'java-'+tag
            results.append({'id':'real_runner_action_java_'+tag,'status':'PASS_CURRENT_STATE_WITNESS',
                'environment':effective,'command':'java','timeout_seconds':5,'output_limit_bytes':4096,
                'returncode':rc,'stdout':stdout,'stderr':stderr})
    assert dict(os.environ) == old
    results.append({'id':'parent_environment_unchanged','status':'PASS_CURRENT_STATE_WITNESS'})
    OUT.write_text(json.dumps({'scope':'current resolver and runner, synthetic action/registry facts only',
        'no_production_changes':True,'results':results},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'result_file':str(OUT),'witnesses':len(results),'status':'PASS'}))

if __name__ == '__main__':
    asyncio.run(main())
