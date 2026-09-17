"""One real controlled host Inspector invocation; no model or installation."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys


def test_real_host_controlled_bcftools_once():
    from agent_core.skill_manager import SkillManager
    from agent_core.runtime_inspection import RuntimeInspectionAdapter
    from agent_core.runtime_probe_contract import build_probe_specification, probe_argv
    from package_manager import PackageManager
    from bio_agents import BioAnalystAgent

    root = Path('/tmp/biomind-gwas-host-controlled')
    candidate = root / 'candidate'
    sid = 'population-genetics/gwas-vcf-prep-qc'
    cap = 'population.genetics.gwas.vcf.prep.qc.inspect_vcf_samples'
    target = 'bio_gatk'
    pm = PackageManager.__new__(PackageManager)
    pm.conda_root = '/public/home/liusiqi/anaconda3'
    # Metadata is a private fixture copy, never the original registry object.
    pm.registry = {'tools': {'bcftools': {'env': target}}, 'environments': {target: {'status': 'registered', 'packages': ['bcftools']}}}
    pm.registry_path = root / 'metadata-copy.json'
    pm.registry_path.write_text(json.dumps(pm.registry, sort_keys=True))
    registry_before = pm.registry_path.read_bytes()
    manager = SkillManager(str(candidate / 'skills'))
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.pm, agent.skill_manager, agent.data_path = pm, manager, str(root / 'task')
    adapter = RuntimeInspectionAdapter(pm, manager, scope_provider=agent._runtime_evidence_scope,
        path_validator=agent._verified_runtime_paths)
    agent.runtime_inspection_adapter = adapter
    requirements = manager.get_runtime_requirements(sid, target_env=target, capability_id=cap)
    prefix = (Path(pm.conda_root) / 'envs' / target).resolve(strict=True)
    specification = build_probe_specification(requirements, prefix)
    assert len(specification['probes']) == 1
    checks = specification['probes'][0]['checks']
    assert [row['argv'] for row in checks] == [[str(prefix / 'bin/bcftools'), '--version'], [str(prefix / 'bin/bcftools'), 'query', '--help']]
    # Pre-generate the one exact production sandbox argv before arming the
    # audit guard. No caller-supplied shell text or host root is admitted.
    expected_argv = adapter.probe.argv({'argv': probe_argv(specification), 'read_roots': [str(prefix)],
        'environment': {'PATH': str(prefix / 'bin') + ':/usr/bin:/bin', 'CONDA_PREFIX': str(prefix),
            'CONDA_DEFAULT_ENV': target, 'LC_ALL': 'C', 'TMPDIR': '/tmp'}})
    assert expected_argv[0] == '/usr/bin/bwrap'
    assert '--bind' not in expected_argv and '--unshare-net' in expected_argv
    bootstrap = sys.modules['__main__']
    bootstrap.HOST_ALLOWED_BWRAP_ARGV = tuple(expected_argv)
    bootstrap.HOST_ALLOWED_BWRAP_REMAINING = 1
    calls = []
    original_run = adapter.probe.run
    async def observe_run(current):
        actual_argv = adapter.probe.argv(current)
        assert actual_argv == expected_argv
        assert not calls
        call = {'argv': actual_argv, 'cwd': '/', 'env': {}, 'specification': deepcopy(current)}
        calls.append(call)
        result = await original_run(current)  # Real bwrap process, unchanged boundary.
        call['raw_process_result'] = deepcopy(result)
        return result
    adapter.probe.run = observe_run
    try:
        result = asyncio.run(agent._discover_skill_runtime_candidates(sid, 'host-controlled-readonly',
            capability_id=cap, target_env=target, use_cache=False))
    finally:
        bootstrap.HOST_ALLOWED_BWRAP_REMAINING = 0
    assert len(calls) <= 1
    assert pm.registry_path.read_bytes() == registry_before
    result_path = root / 'result.json'
    result_path.write_text(json.dumps({'result': result, 'process_calls': calls, 'explicit_target': target,
        'capability_id': cap, 'checks': checks, 'registry_unchanged': True,
        'availability_scope': 'Only bcftools --version and query --help. No VCF inputs or association ran.',
        'source_hashes': {name: hashlib.sha256((candidate / name).read_bytes()).hexdigest() for name in [
            'bio_agents.py', 'package_manager.py', 'agent_core/skill_manager.py', 'agent_core/runtime_inspection.py',
            'agent_core/runtime_probe_contract.py', 'skills/population-genetics/gwas-vcf-prep-qc/SKILL.md',
            'tests/offline_managed_bootstrap.py', 'tests/test_host_controlled_bcftools.py']},
        'boundary': 'Real production discovery/adapter/probe and OS. Private metadata/config only; exact one-shot bwrap audit allowlist; original sandbox and all other guards retained.'},
        ensure_ascii=False, indent=2))
    if result['availability'] != 'AVAILABLE':
        assert result['availability'] == 'UNKNOWN' and not result['install_required']
        assert not result['selected_candidate']
        assert not any(row.get('coverage') for row in result['candidates'])
    print('HOST_CONTROLLED_RESULT=' + str(result_path))
