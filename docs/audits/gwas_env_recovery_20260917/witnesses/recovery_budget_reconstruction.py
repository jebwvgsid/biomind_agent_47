"""Read-only audit driver; writes only its /tmp sandbox and explicit audit outputs.

Stage with stdlib Python; run each worker in a fresh configured Python process.
No API, production import, config/startup import, shell or scientific execution.
Only the explicitly labelled hypothetical case raises the isolated seed limit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from collections.abc import Mapping

REPO = Path('/public/home/liusiqi/03.毕设/Biomind_v1')
FROZEN = Path('/public/home/liusiqi/03.毕设/biomind_audit_exports/gwas_env_recovery_20260917/current_source')
SESSION = Path('/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789648505')
PRIOR = Path('/tmp/biomind-seed-size-1789648505-jko0uzj8')
EVIDENCE = REPO / 'docs/audits/gwas_env_recovery_20260917/evidence'
STATIC_DEPENDENCIES = FROZEN.parent / 'recovery_static_dependencies'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False,
                      default=lambda x: dict(x) if isinstance(x, Mapping) else list(x))


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


def stage():
    root = Path(tempfile.mkdtemp(prefix='biomind-recovery-budget-audit-', dir='/tmp'))
    candidate = root / 'candidate'
    candidate.mkdir()
    # All 78 imported production modules were hash-verified against this
    # persistent source. No earlier /tmp candidate or live repository code.
    manifest = {}
    for source in FROZEN.rglob('*'):
        if source.is_file():
            rel = source.relative_to(FROZEN)
            if 'config' in rel.parts or source.name.startswith('.env'):
                continue
            target = candidate / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            manifest[str(rel)] = digest(target.read_bytes())
    (candidate / 'tests').mkdir(exist_ok=True)
    (candidate / 'tests' / '__init__.py').write_text('')
    bootstrap = STATIC_DEPENDENCIES / 'offline_managed_bootstrap.py'
    dependencies = json.loads((STATIC_DEPENDENCIES / 'manifest.json').read_text())
    bootstrap_bytes = bootstrap.read_bytes()
    assert digest(bootstrap_bytes) == dependencies['files'][bootstrap.name]['sha256']
    (candidate / 'tests' / bootstrap.name).write_bytes(bootstrap_bytes)
    shutil.copy2(__file__, root / 'witness.py')
    copied_session = root / 'data' / 'task_outputs' / SESSION.name
    (copied_session / 'state_contract' / 'objects').mkdir(parents=True)
    original = (SESSION / 'plan_state.json').read_bytes()
    (copied_session / 'plan_state.json').write_bytes(original)
    durable = json.loads(original)
    refs = durable['task_state']['inspection_ref_registry']
    objects = {}
    for entry in refs.values():
        ref = (entry.get('locator') or {}).get('object_ref')
        if not ref:
            continue
        name = ref.removeprefix('object:sha256:') + '.json'
        source = SESSION / 'state_contract' / 'objects' / name
        data = source.read_bytes()
        (copied_session / 'state_contract' / 'objects' / name).write_bytes(data)
        objects[name] = {'sha256': digest(data), 'bytes': len(data), 'source': str(source)}
    # Prefer the preserved report and method. /tmp is only a historical fallback.
    preserved_previous = EVIDENCE / 'recovery_prior_seed_measurement.json'
    if preserved_previous.exists():
        previous = json.loads(preserved_previous.read_text())
        previous_source = str(preserved_previous)
    else:
        previous = {name: json.loads((PRIOR / name).read_text()) for name in
                    ('size_report.json', 'input_identity.json')}
        previous['method_source'] = (PRIOR / 'measure.py').read_text()
        previous_source = str(PRIOR)
    (root / 'prior_measurement.json').write_text(json.dumps(previous, ensure_ascii=False, indent=2))
    (root / 'source_manifest.json').write_text(json.dumps(manifest, indent=2))
    (root / 'input_manifest.json').write_text(json.dumps({
        'original_session_plan': {'source': str(SESSION / 'plan_state.json'), 'sha256': digest(original), 'bytes': len(original)},
        'objects': objects, 'bootstrap_sha256': digest(bootstrap_bytes),
        'bootstrap_source': str(bootstrap), 'prior_measurement_source': previous_source,
        'frozen_root': str(FROZEN), 'head': '82c27867d862ef32d657fbc6162c70d4bc06492c'}, indent=2))
    (root / 'tmp').mkdir()
    print(root)


def bootstrap_worker(audit_root):
    # Reuse the already-reviewed offline bootstrap, stopping before pytest.
    candidate = audit_root / 'candidate'
    code = (candidate / 'tests/offline_managed_bootstrap.py').read_text()
    saved_argv = sys.argv[:]
    sys.argv = [__file__, str(candidate)]
    namespace = {'__name__': '__audit_bootstrap__', '__file__': str(candidate / 'tests/offline_managed_bootstrap.py')}
    exec(compile(code.split('before={str(p.relative_to(C))', 1)[0], namespace['__file__'], 'exec'), namespace)
    sys.argv = saved_argv
    return candidate


def worker(audit_root, mode):
    candidate = bootstrap_worker(audit_root)
    import asyncio
    from copy import deepcopy
    from dataclasses import replace
    from langchain_core.messages import AIMessage, HumanMessage
    from agent_core.plan_tracker import plan_record_hash
    from agent_core.workflow import recovery_agent as ra
    from agent_core.workflow.context_compaction import (
        ContextBudgetPolicy, ReferenceTokenEstimator, prepare_compaction,
        measure_compactor_wire_tokens, ContextCompactionError,
    )
    from agent_core.workflow.recovery_projection import project_recovery_seed
    from agent_core.workflow.recovery_tools import build_recovery_inspection_registry, RecoveryLocalToolDispatcher
    from agent_core.workflow.recovery_node import Schema2RecoveryNode, _normalize_inherited_materials, _build_compaction_request
    from agent_core.workflow.inspection_evidence import InspectionObjectReader, inspection_scope
    from agent_core.workflow.phase_archive import PhaseArchiveStore
    from agent_core.workflow.recovery_context import RecoveryObservationStore, archive_recovery_episode
    from agent_core.workflow.scoped_context import provider_visible_payload

    data_path = audit_root / 'data'
    plan_path = data_path / 'task_outputs' / SESSION.name / 'plan_state.json'
    final_durable = json.loads(plan_path.read_text())
    durable = deepcopy(final_durable)
    entries = durable['task_state']['inspection_ref_registry']
    record = next(iter(durable['task_state']['causal_failure_packets'].values()))
    plan_entry = next(x for x in entries.values() if x['scope']['object_kind'] == 'plan')
    name = plan_entry['locator']['object_ref'].removeprefix('object:sha256:') + '.json'
    prior_plan = json.loads((plan_path.parent / 'state_contract/objects' / name).read_text())['payload']['body']['approved_plan']
    expected_hash = record['failure_packet']['plan_spec_ref']['plan_record_hash']
    assert plan_record_hash(prior_plan) == expected_hash
    durable['current_plan']['plan_spec'] = prior_plan
    durable['current_plan']['plan_record_hash'] = expected_hash
    projection = project_recovery_seed({'task_id': durable['task_id']}, durable)
    seed = projection.recovery_seed
    phase_store = PhaseArchiveStore(data_path)
    registry = build_recovery_inspection_registry(seed, durable, phase_store=phase_store)
    dispatcher = RecoveryLocalToolDispatcher(seed=seed, durable=durable, registry=registry, phase_store=phase_store)
    scope = inspection_scope(seed.failure_packet, seed.seed_id)
    reader = InspectionObjectReader(str(data_path), scope=scope)
    policy = ContextBudgetPolicy(application_context_limit_tokens=200000, output_reserve_tokens=8192,
                                 estimation_margin_tokens=4096, safety_headroom_tokens=2048)
    estimator = ReferenceTokenEstimator()
    report = {'mode': mode, 'pid': os.getpid(), 'scope': scope, 'policy': policy.to_dict(),
              'estimator': {'name': estimator.name, 'version': estimator.version, 'exact_or_estimated': estimator.exact_or_estimated},
              'producer_plan_hash_verified': expected_hash, 'hypothesis': False}

    def measured(value):
        text = canonical(value)
        return {'chars': len(text), 'utf8_bytes': len(text.encode()), 'sha256': digest(value),
                'estimated_tokens': estimator.estimate(text).tokens}

    def failure(exc):
        return {'type': type(exc).__name__, 'code': str(getattr(exc, 'code', '')), 'detail': str(getattr(exc, 'detail', str(exc)))}

    def read_refs():
        rows = []
        for ref, entry in entries.items():
            row = {'ref_id': ref, 'object_kind': entry['scope']['object_kind'],
                   'digest': entry['digest'], 'revision': entry['scope'].get('object_revision'),
                   'scope': entry['scope'], 'locator': entry.get('locator')}
            try:
                body = reader.read_body(entry, entry.get('locator') or {})
                page = reader.read(entry, entry.get('locator') or {}, start_line=1, end_line=8)
                row.update(status='read', body=measured(body), page={k: v for k, v in page.items() if k != 'content'},
                           rc0_preserved='"returncode":0' in canonical(body),
                           exact_validator_code_preserved='OBSERVED_ARTIFACT_CONTENT_INVALID' in canonical(body))
            except Exception as exc:
                row.update(status='denied_or_unavailable', error=failure(exc))
            rows.append(row)
        wrong = InspectionObjectReader(str(data_path), scope={**scope, 'attempt_id': 'audit-wrong-attempt'})
        try:
            wrong.read_body(plan_entry, plan_entry['locator'])
            report['wrong_attempt'] = {'unexpected': 'read'}
        except Exception as exc:
            report['wrong_attempt'] = failure(exc)
        report['descriptor_tamper'] = []
        for label in ('revision', 'tools', 'digest'):
            altered = deepcopy(plan_entry)
            if label == 'revision':
                altered['scope']['object_revision'] += 1
            elif label == 'tools':
                altered['scope']['allowed_tools'] = ['artifact.read']
            else:
                altered['digest'] = 'sha256:' + '0' * 64
            try:
                reader.read_body(altered, altered['locator'])
                report['descriptor_tamper'].append({'case': label, 'unexpected': 'read'})
            except Exception as exc:
                report['descriptor_tamper'].append({'case': label, 'error': failure(exc)})
        report['persistent_reads'] = rows

    read_refs()
    if mode == 'restart':
        extfile = audit_root / 'externalized_observation.json'
        if extfile.exists():
            ext = json.loads(extfile.read_text())
            archive = phase_store.read(ext['observation_ref'])
            report['externalized_store_restart'] = {'archive_ref': archive.archive_ref,
                'content_sha256': archive.summary['content_sha256'], 'payload': measured(archive.summary['result'])}
            for tool, args in [('evidence.read', {'evidence_ref': ext['observation_ref']}),
                               ('step.inspect', {'ref_id': ext['observation_ref'], 'section': 'summary'})]:
                try:
                    dispatcher.dispatch(tool, args)
                    report.setdefault('externalized_tool_restart', []).append({'tool': tool, 'status': 'read'})
                except Exception as exc:
                    report.setdefault('externalized_tool_restart', []).append({'tool': tool, 'error': failure(exc)})
    else:
        node = object.__new__(Schema2RecoveryNode)
        node.data_path = str(data_path)
        node.inherited_materials_factory = None
        materials = node._initial_inherited_materials(seed=seed, durable=durable)
        inherited, descriptors = _normalize_inherited_materials(materials, epoch_id=seed.seed_id + ':context-epoch:0')
        report['inherited_materials'] = [{k: v for k, v in x.items() if k not in {'body', 'summary'}} |
                                      {'body': measured(x.get('body', ''))} for x in materials]
        captured = {}

        def trace(frame, event, arg):
            if frame.f_code.co_name == 'project_recovery_seed_for_provider' and event == 'exception' and 'frozen' in frame.f_locals:
                captured['seed'] = deepcopy(frame.f_locals['frozen'])
            return trace

        sys.settrace(trace)
        try:
            ra.project_recovery_seed_for_provider(seed, durable, inspection_manifest=registry.provider_manifest())
        except Exception as exc:
            report['original_seed_gate'] = failure(exc)
        finally:
            sys.settrace(None)
        provider_seed = captured['seed']
        assert len(canonical(provider_seed)) == 32097
        report['seed'] = measured(provider_seed)
        report['seed_parts'] = {k: measured(v) for k, v in provider_seed.items()}
        report['manifest'] = registry.provider_manifest()

        def directive():
            return AIMessage(content=canonical({'schema_version': 1, 'directive_id': 'audit-only-blocked',
                'task_id': seed.failure_packet.task_id, 'action': 'blocked',
                'summary': 'Offline capture ends here; rc=0, artifact validation rejected, no Commit. Q row correspondence remains unknown.',
                'reason_code': 'AUDIT_CAPTURE_ONLY', 'evidence_refs': [seed.failure_packet.source_outcome_ref]}))

        class CaptureModel:
            def __init__(self, tool_first=False):
                self.requests = []
                self.schemas = []
                self.tool_first = tool_first
            def bind_tools(self, schemas):
                self.schemas = deepcopy(schemas)
                return self
            async def ainvoke(self, messages):
                self.requests.append([provider_visible_payload(m) for m in messages])
                if self.tool_first and len(self.requests) == 1:
                    return AIMessage(content='', tool_calls=[{'id': 'audit-large-tool', 'type': 'tool_call',
                        'name': 'failure_inspect', 'args': {'ref_id': seed.failure_packet.source_outcome_ref}}])
                return directive()

        async def capture_cases():
            original_limit = ra.RECOVERY_SEED_HARD_LIMIT_CHARS
            # Explicit hypothetical branch ONLY; no source file is changed.
            ra.RECOVERY_SEED_HARD_LIMIT_CHARS = 10**9
            report['hypothesis'] = 'Only isolated module seed char limit raised; real assembly/reader/budget/loop unchanged.'
            try:
                model = CaptureModel()
                episode = await ra.RecoveryAgentLoop(model=model, dispatcher=dispatcher, context_budget_policy=policy,
                    token_estimator=estimator, observation_store=RecoveryObservationStore(phase_store)).ainvoke(
                    seed=seed, durable=durable, inspection_manifest=registry.provider_manifest(), inherited_messages=inherited)
                report['complete_request'] = {'requests': len(model.requests), 'audit': episode['audit'],
                    'terminal_error': episode['terminal_error'], 'compaction_required': episode['compaction_required'],
                    'messages': [measured(x) | {'type': x.get('type'), 'ordinal': i} for i, x in enumerate(model.requests[0])] if model.requests else [],
                    'tools': measured(model.schemas), 'tool_count': len(model.schemas)}
                (audit_root / 'request_capture.json').write_text(json.dumps({'hypothesis': report['hypothesis'],
                    'policy': policy.to_dict(), 'messages': model.requests[0] if model.requests else [], 'tools': model.schemas}, ensure_ascii=False, indent=2))
                # Small seed / large inherited input: current gate unchanged for this genuinely small projection.
                small = replace(seed, failure_packet=replace(seed.failure_packet, diagnostic_facts=(), inspection_refs=()))
                big = HumanMessage(content='audited synthetic inherited evidence; unresolved Q correspondence.\n' * 12000,
                                   id='audit-large-inherited')
                first_model = CaptureModel()
                large_episode = await ra.RecoveryAgentLoop(model=first_model, dispatcher=dispatcher, context_budget_policy=policy,
                    token_estimator=estimator).ainvoke(seed=small, durable=durable, inherited_messages=[big])
                report['first_inherited_pressure'] = {'model_calls': len(first_model.requests),
                    'small_seed_chars': large_episode['audit']['provider_seed_chars'], 'boundary': large_episode['compaction_required'],
                    'terminal_error': large_episode['terminal_error'], 'budget_checks': large_episode['audit']['context_budget_checks']}
                # Large external tool result is the sole synthetic leaf; externalization and following admission are real.
                class LargeDispatcher:
                    def dispatch(self, tool, arguments):
                        return {'status': 'OK', 'ref_id': seed.failure_packet.source_outcome_ref,
                                'content': 'synthetic diagnostic detail; raw cause remains rc=0 and rejected artifact.\n' * 4000}
                tool_model = CaptureModel(tool_first=True)
                tool_episode = await ra.RecoveryAgentLoop(model=tool_model, dispatcher=LargeDispatcher(), context_budget_policy=policy,
                    token_estimator=estimator, observation_store=RecoveryObservationStore(phase_store)).ainvoke(
                    seed=small, durable=durable)
                observations = tool_episode['_observation_receipts']
                report['large_tool'] = {'model_calls': len(tool_model.requests), 'budget_checks': tool_episode['audit']['context_budget_checks'],
                    'observations': observations, 'terminal_error': tool_episode['terminal_error']}
                if observations and observations[0]['result'].get('observation_ref'):
                    ext = observations[0]['result']
                    (audit_root / 'externalized_observation.json').write_text(json.dumps(ext))
                    report['large_tool']['original_archive_payload'] = measured(phase_store.read(ext['observation_ref']).summary['result'])
                    for tool, args in [('evidence.read', {'evidence_ref': ext['observation_ref']}),
                                       ('step.inspect', {'ref_id': ext['observation_ref'], 'section': 'summary'})]:
                        try:
                            dispatcher.dispatch(tool, args)
                            report['large_tool'].setdefault('readback', []).append({'tool': tool, 'status': 'read'})
                        except Exception as exc:
                            report['large_tool'].setdefault('readback', []).append({'tool': tool, 'error': failure(exc)})
                # Growth crossing a measured next-call limit while first call itself fits.
                first_tokens = tool_episode['audit']['context_budget_checks'][0]['budget']['estimated_input_tokens']
                next_tokens = tool_episode['audit']['context_budget_checks'][1]['budget']['estimated_input_tokens']
                narrow_policy = replace(policy, application_context_limit_tokens=first_tokens + policy.output_reserve_tokens + policy.estimation_margin_tokens + max(1, (next_tokens-first_tokens)//2), trigger_ratio=1.0, safety_headroom_tokens=0)
                growth_model = CaptureModel(tool_first=True)
                growth = await ra.RecoveryAgentLoop(model=growth_model, dispatcher=LargeDispatcher(), context_budget_policy=narrow_policy,
                    token_estimator=estimator, observation_store=RecoveryObservationStore(phase_store)).ainvoke(seed=small, durable=durable)
                report['next_request_pressure'] = {'synthetic_reduced_capacity': True, 'model_calls': len(growth_model.requests),
                    'boundary': growth['compaction_required'], 'budget_checks': growth['audit']['context_budget_checks']}
                # Use the real Recovery compaction adapter and core, with only bounded compactor responses substituted.
                req, bodies, originals = _build_compaction_request(seed=small, episode=large_episode,
                    inherited_messages=large_episode['_inherited_messages'], local_messages=large_episode['_local_messages'],
                    inherited_descriptors={}, source_epoch_id=seed.seed_id + ':large-first', history_version=0,
                    selected_revision=seed.task_spec_ref.task_revision, policy=policy, estimator=estimator)
                source_archive = archive_recovery_episode(phase_store=phase_store, seed=small,
                    episode=large_episode, overflow_epoch=0, linked_previous_archive_ref='')
                req = replace(req, source_archive_ref=source_archive.archive_ref)
                calls = []
                async def fake_compactor(messages):
                    wire_tokens = measure_compactor_wire_tokens(messages, policy=policy, estimator=estimator)
                    text = messages[1]['content']; envelope = json.loads(text[text.index('\n')+1:text.rindex('\n')])
                    payload = envelope['request']; op = payload.get('operation', 'selector')
                    calls.append({'operation': op, 'estimated_tokens': wire_tokens, 'safe_input': policy.safe_input_tokens,
                                  'wire_chars': len(canonical(messages)), 'sha256': digest(messages)})
                    assert wire_tokens <= policy.safe_input_tokens
                    identity = {k: payload[k] for k in ('schema_version', 'compaction_request_id', 'source_epoch_id', 'source_manifest_hash')}
                    if op == 'map_source_segment':
                        parent, segment = payload['parent_source_unit'], payload['segment_source_unit']
                        return {**identity, 'operation': op, 'segment_unit_id': segment['unit_id'], 'parent_source_unit_id': parent['unit_id'],
                            'parent_source_content_hash': parent['content_hash'], 'segment_content_hash': segment['content_hash'],
                            'coverage': segment['coverage'], 'summary': 'Synthetic evidence; Q correspondence unresolved.',
                            'missing_details': 'Exact bytes remain in source ranges.'}
                    if op == 'merge_segment_summaries':
                        return {**identity, **{k: payload[k] for k in ('operation', 'merge_request_id', 'input_summary_ids', 'input_summary_hashes', 'coverage_spans')},
                            'summary': 'Synthetic evidence; Q correspondence unresolved.'}
                    units = payload['source_units']
                    return {**identity, 'mode': 'normal', 'retain_exact_unit_ids': [],
                        'summary_source_unit_ids': [x['unit_id'] for x in units],
                        'summary': 'Audit synthetic context: rc=0, artifact rejected, Q correspondence remains unresolved.',
                        'material_retrieval_state': [], 'unresolved_items': ['Q correspondence unknown'], 'selection_rationale': 'Offline bounded witness.'}
                maintenance = {}
                try:
                    compacted = await prepare_compaction(req, bodies, fake_compactor, estimator=estimator, maintenance_state=maintenance)
                    report['compactor'] = {'status': 'prepared', 'calls': calls, 'maintenance': maintenance,
                        'hierarchy': compacted.hierarchy, 'initial_budget': compacted.initial_budget.to_dict(),
                        'final_budget': compacted.epoch.budget.to_dict()}
                    invalid_calls = []
                    async def invalid_compactor(messages):
                        invalid_calls.append({'wire_tokens': measure_compactor_wire_tokens(messages, policy=policy, estimator=estimator)})
                        return {'invalid': 'bounded invalid selector; no source identities'}
                    invalid_maintenance = deepcopy(maintenance)
                    try:
                        await prepare_compaction(req, bodies, invalid_compactor, estimator=estimator,
                            maintenance_state=invalid_maintenance)
                        report['compactor_invalid_retry'] = {'unexpected': 'prepared'}
                    except Exception as exc:
                        report['compactor_invalid_retry'] = {'calls': invalid_calls, 'error': failure(exc),
                            'charged_tokens': invalid_maintenance.get('charged_tokens')}
                    exhausted_calls = []
                    async def exhausted_compactor(messages):
                        exhausted_calls.append(True)
                        return {}
                    try:
                        await prepare_compaction(req, bodies, exhausted_compactor, estimator=estimator,
                            maintenance_state={**deepcopy(maintenance), 'charged_tokens': 400000})
                        report['compactor_exhausted'] = {'unexpected': 'prepared'}
                    except Exception as exc:
                        report['compactor_exhausted'] = {'calls': len(exhausted_calls), 'error': failure(exc)}
                except Exception as exc:
                    report['compactor'] = {'status': 'stopped', 'error': failure(exc), 'calls': calls, 'maintenance': maintenance}
                # Fixed necessary context cannot be removed by compactor; prove finite refusal before business model.
                fixed_policy = replace(policy, application_context_limit_tokens=15000)
                fixed_model = CaptureModel()
                fixed = await ra.RecoveryAgentLoop(model=fixed_model, dispatcher=dispatcher, context_budget_policy=fixed_policy,
                    token_estimator=estimator).ainvoke(seed=small, durable=durable)
                report['fixed_context_limit'] = {'synthetic_capacity': 15000, 'model_calls': len(fixed_model.requests),
                    'terminal_error': fixed['terminal_error'], 'budget_checks': fixed['audit']['context_budget_checks']}
            finally:
                ra.RECOVERY_SEED_HARD_LIMIT_CHARS = original_limit
            report['production_limit_restored_in_worker'] = ra.RECOVERY_SEED_HARD_LIMIT_CHARS

        asyncio.run(capture_cases())
    manifest = json.loads((audit_root / 'source_manifest.json').read_text())
    imported = {}
    for name, module in list(sys.modules.items()):
        file = getattr(module, '__file__', None)
        if file and str(file).startswith(str(candidate)):
            rel = str(Path(file).relative_to(candidate))
            imported[rel] = {'sha256': digest(Path(file).read_bytes()), 'in_frozen_manifest': rel in manifest,
                             'matches_frozen': digest(Path(file).read_bytes()) == manifest.get(rel)}
    report['imported_sources'] = imported
    report['copied_session_plan_unchanged'] = digest(plan_path.read_bytes()) == json.loads((audit_root / 'input_manifest.json').read_text())['original_session_plan']['sha256']
    (audit_root / (mode + '_report.json')).write_text(json.dumps(json.loads(canonical(report)), ensure_ascii=False, indent=2))
    print(json.dumps({'mode': mode, 'report': str(audit_root / (mode + '_report.json')),
                      'seed': report.get('seed'), 'complete_request_count': report.get('complete_request', {}).get('requests'),
                      'copied_session_plan_unchanged': report['copied_session_plan_unchanged']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('stage', 'capture', 'restart'))
    parser.add_argument('--sandbox')
    args = parser.parse_args()
    if args.mode == 'stage':
        stage()
    else:
        worker(Path(args.sandbox).resolve(), args.mode)
