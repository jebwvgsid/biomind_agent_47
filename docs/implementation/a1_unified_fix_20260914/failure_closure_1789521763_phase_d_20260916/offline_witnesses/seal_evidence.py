"""Archive only this audit's guarded /tmp results and verify protected bytes.

No production imports; no credentials/configuration/scientific input reads.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

HERE = Path(__file__).resolve().parent
AUDIT = HERE.parent
REPO = next(p for p in HERE.parents if (p / 'bio_agents.py').is_file())
RUNS = [('run01', 'results'), ('run02', 'results'), ('run02', 'loops'), ('run03', 'results')]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    return subprocess.check_output(['git', *args], cwd=REPO).decode()


def main():
    baseline = json.loads((AUDIT / 'protection_before.json').read_text())
    drift = [name for name, digest in baseline['safe_source_sha256'].items()
             if not (REPO / name).is_file() or sha((REPO / name).read_bytes()) != digest]
    assert not drift, drift
    assert git('rev-parse', 'HEAD').strip() == baseline['base']
    assert sha(git('ls-files', '--stage', '-z').encode()) == baseline['index_sha256'], 'Index changed before staging.'
    indexes = []
    latest = {}
    members = {}
    loaded = {}
    for run, result in RUNS:
        root = Path('/tmp/bm_failure_closure_d_' + run)
        directory = root / result
        reports = json.loads((directory / 'pytest-results.json').read_text())
        calls = [r for r in reports if r.get('phase') == 'call']
        call_outcomes = Counter(r['outcome'] for r in calls)
        for row in calls:
            # Newer current-byte results replace earlier observations; do not sum reruns.
            latest[row['nodeid']] = {'run': run + '/' + result, 'outcome': row['outcome']}
        stdout = (directory / 'stdout.txt').read_text()
        identities = {}
        for line in stdout.splitlines():
            if line.startswith('IMPORTED_SOURCES '):
                for module, name in json.loads(line.split(' ', 1)[1]).items():
                    path = Path(name)
                    assert path.is_relative_to(root / 'export')
                    identities[module] = {'path': str(path.relative_to(root / 'export')), 'sha256': sha(path.read_bytes())}
        loaded[run + '/' + result] = identities
        entries = []
        for path in sorted(directory.rglob('*')):
            rel = path.relative_to(directory)
            if rel.parts[0] == 'scratch' or not path.is_file() or path.is_symlink():
                continue
            assert not path.name.startswith('.env') and path.name != '启动.sh'
            target = run + '/' + result + '/' + str(rel)
            members[target] = path
            entries.append({'path': target, 'bytes': path.stat().st_size, 'sha256': sha(path.read_bytes())})
        manifest = root / 'export/export_manifest.json'
        members[run + '/export_manifest.json'] = manifest
        indexes.append({'run': run + '/' + result, 'call_outcomes': dict(call_outcomes),
                        'source_unchanged': 'SOURCE_UNCHANGED True' in stdout,
                        'loaded_modules_observed': len(identities),
                        'result': 'TEST_WATCHDOG_360_SECONDS' if run == 'run01' else ('PASS' if not call_outcomes.get('failed') else 'FIXTURE_FAILURES_RETAINED'),
                        'archive_members': entries})
    archive = HERE / 'guarded_results.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for target, source in sorted(members.items()):
            tar.add(source, arcname=target, recursive=False)
    (HERE / 'loaded_sources.json').write_text(json.dumps(loaded, ensure_ascii=False, indent=2) + '\n')
    new = {k: v for k, v in latest.items() if k.startswith('tests/phase_d_1789521763/')}
    old = {k: v for k, v in latest.items() if k.startswith('tests/test_observation_warning_recovery_stop.py')}
    assert len(new) == 73 and all(v['outcome'] == 'passed' for v in new.values()), new
    assert len(old) == 11 and all(v['outcome'] == 'passed' for v in old.values()), old
    index = {'runs': indexes, 'final_unique_new_witnesses': new, 'final_unique_existing_regressions': old,
             'count_policy': 'Latest affected witness byte version; unchanged graph functions reused from run02. No rerun double count.',
             'archive': {'path': archive.name, 'sha256': sha(archive.read_bytes()), 'bytes': archive.stat().st_size}}
    (HERE / 'results_index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n')
    verification = {
        'document_id': 'BM-FAILURE-CLOSURE-1789521763-V1', 'stage': 'D', 'stage_E': 'NOT_AUTHORIZED_NOT_IMPLEMENTED',
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'BASE': baseline['base'], 'FINAL_production_baseline': baseline['base'],
        'audit_commit': 'Full artifact commit hash supplied in Git history and final delivery; no self-referential hash.',
        'branch': baseline['branch'], 'production_modified': False, 'original_tests_modified': False,
        'protected_safe_source_files': len(baseline['safe_source_sha256']), 'protected_byte_drift': drift,
        'original_index_unchanged_before_staging': True,
        'configuration_protection': 'Metadata only; no secret/config-content hash claim.',
        'current_worktree_not_bare_HEAD_deployment': True,
        'offline_new_unique': {'passed': len(new), 'failed': 0, 'meaning': 'Current-behavior witnesses, including DEFECT_PRESENT; not production FIX PASS'},
        'offline_existing_warning_stop_unique': {'passed': len(old), 'failed': 0},
        'development_runs': [{'run': i['run'], 'call_outcomes': i['call_outcomes'], 'result': i['result']} for i in indexes],
        'real_calls': {'model_api': 0, 'scientific_tools': 0, 'original_task_execute': 0},
        'synthetic_call_evidence': {
            'binding_local_correction_graph': {'generate': 7, 'reviewer': 6, 'recovery': 0, 'approval': 6, 'execute': 6, 'commit': 6},
            'unresolved_binding_graph': {'generate': 2, 'reviewer': 1, 'recovery': 0, 'execute': 1, 'commit': 1, 'bad_candidate_execute': 0, 'stop': 'second real approval interrupt'},
            'rc0_postcondition_graph': {'generate': 2, 'reviewer': 1, 'recovery': 1, 'execute': 2, 'commit': 1, 'prepare_commit': 0},
            'continuous_pages': {'model_calls': 12, 'tool_calls': 12, 'reported_synthetic_tokens': 1202400, 'stop': 'RECOVERY_LLM_TURN_LIMIT'},
            'compaction': {'business_calls': 2, 'maintenance_calls': 3, 'reported_synthetic_maintenance_tokens': 810, 'summary_in_next_wire': True},
        },
        'history_usage': {'recovery_input': 496449, 'recovery_output': 14836, 'recovery_known_total': 511285, 'cancelled_request_usage': 'UNKNOWN'},
        'unknown_or_not_run': {'real_model_canary': 'NOT_RUN', 'real_cache_remeasurement': 'NOT_RUN', 'science_e2e': 'NOT_RUN',
                              'remote_cancel_or_billing_stop': 'UNKNOWN', 'historical_OS_exit_code': 'UNKNOWN',
                              'all_V19_interleavings': 'NOT_RUN; mapping retained for E', 'all_reachable_failure_variants_dynamic': 'NOT_RUN; source/UNKNOWN rows explicitly mapped'},
        'commit_scope': str(AUDIT.relative_to(REPO)) + '/',
        'safety': {'original_session': 'NOT_RESUMED', 'credentials_startup_scientific_inputs': 'NOT_READ', 'installation': 'NOT_PERFORMED', 'push': 'NOT_PERFORMED'},
        'test_commands_and_raw_results': 'offline_witnesses/guarded_results.tar.gz: */invocation.json, */stdout.txt, */pytest-results.json',
        'loaded_source_receipts': 'offline_witnesses/loaded_sources.json',
        'result_index': 'offline_witnesses/results_index.json',
    }
    (AUDIT / 'verification.json').write_text(json.dumps(verification, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'new_unique': len(new), 'old_unique': len(old), 'protected_files': len(baseline['safe_source_sha256']),
                      'archive_bytes': archive.stat().st_size, 'source_drift': drift}))


if __name__ == '__main__':
    main()
