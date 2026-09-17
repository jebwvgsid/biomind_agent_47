#!/usr/bin/env python3
"""Read formal inputs and replay the approved check in an isolated output root.

No application startup, provider, installation, or original-session writes.
The replay PATH contains real standard utilities only: scientific tools cannot run.
"""
from pathlib import Path
import gzip
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

AUDIT = Path(__file__).resolve().parents[1]
REPO = AUDIT.parents[2]
PROJECT = REPO.parent
SESSION = PROJECT / 'benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789648505'
DATA = PROJECT / 'benchmarks/01.测试/04.重测序/Part6.GWAS/data'
ATTEMPT = SESSION / 'attempts/attempt_79e2ff04f7dcfa103d11'
FROZEN = Path(json.loads((AUDIT / 'evidence/baseline_current.json').read_text())['source_root'])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    pheno_path = DATA / 'Real_trait.C16_0.table'
    q_path = DATA / 'Real_snp.3.Q'
    pheno = [line.split() for line in pheno_path.read_text().splitlines() if line.strip()]
    q = [line.split() for line in q_path.read_text().splitlines() if line.strip()]
    header_bytes = bytearray()
    with gzip.open(DATA / 'Real_snp.vcf.gz', 'rb') as fh:
        for line in fh:
            header_bytes.extend(line)
            if line.startswith(b'#CHROM\t'):
                vcf_ids = line.decode().rstrip('\r\n').split('\t')[9:]
                break
            if len(header_bytes) > 2_000_000:
                raise RuntimeError('bounded header inspection exceeded')
        else:
            raise RuntimeError('VCF header absent')
    pheno_ids = [r[0] for r in pheno]
    formal = {}
    for name in ['Real_snp.vcf.gz', 'Real_trait.C16_0.table', 'Real_snp.3.Q', 'Real.gff3']:
        path = DATA / name
        stat = path.stat()
        formal[name] = {'path': str(path), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
        if stat.st_size < 100_000:
            formal[name]['sha256'] = digest(path.read_bytes())
    input_facts = {
        'formal_inputs': formal,
        'vcf': {'samples': len(vcf_ids), 'unique_samples': len(set(vcf_ids)),
                'header_bytes_read': len(header_bytes), 'header_sha256': digest(header_bytes),
                'sample_list_sha256': digest(('\n'.join(vcf_ids) + '\n').encode()),
                'scope': 'header only; variant records were not analyzed'},
        'phenotype': {'rows': len(pheno), 'widths': sorted({len(r) for r in pheno}),
                      'first_row': pheno[0], 'unique_ids': len(set(pheno_ids)),
                      'all_trait_values_numeric': all(is_number(r[1]) for r in pheno),
                      'header_observation': 'first row has same sample/value structure and its ID is first VCF sample'},
        'vcf_phenotype_same_id_set': set(vcf_ids) == set(pheno_ids),
        'vcf_phenotype_same_order': vcf_ids == pheno_ids,
        'q': {'rows': len(q), 'widths': sorted({len(r) for r in q}), 'first_row': q[0],
              'all_cells_numeric': all(is_number(v) for r in q for v in r),
              'sample_id_column_observed': False, 'sample_order_confirmed': False,
              'reason': 'No ID, order metadata or generating provenance in supplied Q; equal row counts are not identity evidence.'},
        'provenance_search': {'scope': 'four formal files plus names in their immediate data directory',
                              'adjacent_names': sorted(p.name for p in DATA.iterdir()),
                              'boundary': 'Adjacent .fam/other phenotypes were not adopted as task inputs or Q-order evidence.'},
    }
    script = (ATTEMPT / 'approved.sh').read_text()
    with tempfile.TemporaryDirectory(prefix='biomind-gwas-input-audit-') as tmp:
        root = Path(tmp)
        utilities = root / 'bin'
        utilities.mkdir()
        for name in ['mkdir', 'stat', 'head', 'cat', 'awk', 'wc']:
            target = Path('/usr/bin') / name
            if not target.exists(): target = Path('/bin') / name
            assert target.exists(), name
            (utilities / name).symlink_to(target)
        replacements = {
            str(SESSION / 'plan_artifacts/00.input_check'): str(root / 'checks'),
            str(SESSION / 'plan_artifacts/logs'): str(root / 'logs'),
        }
        isolated = script
        for old, new in replacements.items(): isolated = isolated.replace(old, new)
        assert str(SESSION) not in isolated
        run = subprocess.run(['/bin/bash', '-c', isolated], env={'PATH': str(utilities), 'LC_ALL': 'C'},
                             cwd=root, text=True, capture_output=True, timeout=15)
        artifacts = {}
        for name in ['vcf.samples.txt', 'phenotype.samples.txt', 'q.samples.txt', 'dependency_status.tsv']:
            p = root / 'checks' / name
            b = p.read_bytes()
            original = SESSION / 'plan_artifacts/00.input_check' / name
            artifacts[name] = {'size': len(b), 'lines': len(b.splitlines()), 'sha256': digest(b),
                               'same_as_original_artifact': original.is_file() and original.read_bytes() == b}
        replay = {'layer': 'actual approved Bash, only output paths relocated; real standard utilities',
                  'source': str(ATTEMPT / 'approved.sh'), 'source_sha256': digest(script.encode()),
                  'substitutions': {'CHECK_DIR': '<isolated>/checks', 'LOGS_DIR': '<isolated>/logs'},
                  'controlled_condition': 'PATH deliberately excludes all scientific tools; no fake scientific tool results',
                  'returncode': run.returncode, 'stdout': run.stdout, 'stderr': run.stderr,
                  'artifacts': artifacts, 'meaning': 'Current defect reproduced, not a repaired workflow'}
        assert run.returncode == 0
        assert artifacts['vcf.samples.txt']['size'] == 0
        assert artifacts['phenotype.samples.txt']['lines'] == len(pheno) - 1
        assert artifacts['q.samples.txt']['lines'] == len(q)
    helper_path = FROZEN / 'skills/population-genetics/gwas-vcf-prep-qc/scripts/sort_phenotype.py'
    helper = load_module('audit_gwas_sort_phenotype', helper_path)
    h, rows = helper.read_table(pheno_path)
    qh, qr = helper.read_table(q_path)
    helper_facts = {'path': str(helper_path), 'sha256': digest(helper_path.read_bytes()),
                    'called': 'read_table only; no output writers or main()',
                    'phenotype_header': h, 'phenotype_rows_preserved': len(rows),
                    'q_header': qh, 'q_dictionary_keys_count': len(qr),
                    'q_keys_are_numeric_first_column': all(is_number(k) for k in qr),
                    'boundary': 'Helper supports this headerless phenotype, but raw numeric Q is still unsafe without validated IDs.'}
    assert h is None and len(rows) == len(pheno)
    planner = inspect_planner()
    out = {'input_facts': input_facts, 'isolated_replay': replay, 'existing_helper': helper_facts,
           'planner_and_provider': planner, 'checks': 'PASS: observations and expected current defects reproduced'}
    (AUDIT / 'evidence/input_planner.json').write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'vcf': len(vcf_ids), 'phenotype': len(pheno), 'q_shape': [len(q), len(q[0])],
                      'id_and_order_equal': vcf_ids == pheno_ids, 'q_order': 'unknown',
                      'replay_rc': replay['returncode'], 'artifacts': artifacts,
                      'planner_compiler_results': planner['ask_effect_compiler']}, ensure_ascii=False))


def is_number(value):
    try:
        float(value)
        return True
    except ValueError:
        return False


def inspect_planner():
    traces = SESSION / 'context_trace'
    candidates = []
    for p in sorted(traces.glob('planner_initial-*/response.json')):
        doc = json.loads(json.loads(p.read_text())['content'])
        candidates.append({'path': str(p), 'sha256': digest(p.read_bytes()),
                           'check_inputs': doc['steps'][0], 'ask_step': doc['steps'][2],
                           'proposed_artifacts': doc['proposed_artifacts']})
    rejections = []
    last = sorted(traces.glob('planner_initial-*/request.json'))[-1]
    messages = json.loads(last.read_text())['messages']
    for m in messages:
        try: value = json.loads(m['content'])
        except (ValueError, TypeError): continue
        if isinstance(value, dict) and value.get('code') == 'PLANNER_CANDIDATE_REJECTED':
            rejections.append(value['validation_issues'])
    sys.path.insert(0, str(FROZEN))
    from agent_core.step_effects import compile_step_effect
    results = []
    for c in candidates:
        result = compile_step_effect(c['ask_step'], {}, step_index=2)
        results.append({'complete': result['complete'], 'codes': [v['code'] for v in result['issues']],
                        'effect': result['effect']})
    assert results[0]['codes'] == ['PLAN_STEP_EFFECT_UNSPECIFIED']
    assert results[1]['codes'] == ['STEP_EFFECT_PROTOCOL_INVALID']
    assert results[2]['complete']
    # Actual message evidence, not newly assembled state.
    provider = []
    first_pheno_id = (DATA / 'Real_trait.C16_0.table').read_text().split()[0]
    first_q_values = (DATA / 'Real_snp.3.Q').read_text().splitlines()[0].split()
    for p in sorted(traces.glob('*/request.json')):
        if not p.parent.name.startswith(('consultant-', 'planner_initial-', 'generate-')): continue
        mm = json.loads(p.read_text())['messages']
        joined = '\n'.join(str(m['content']) for m in mm)
        provider.append({'path': str(p), 'sha256': digest(p.read_bytes()),
                         'message_count': len(mm),
                         'literal_first_pheno_id_present': first_pheno_id in joined,
                         'all_literal_first_q_values_present': all(v in joined for v in first_q_values),
                         'warns_q_without_sample_column': 'Q matrix lacks sample column' in joined,
                         'forbids_invented_files_for_checks': 'never invent a file merely to satisfy review' in joined,
                         'requires_typed_side_effect': 'Inline side effects must include a typed' in joined})
    consult_actions = []
    for p in sorted(traces.glob('consultant-*/response.json')):
        value = json.loads(json.loads(p.read_text())['content'])
        consult_actions.append({'path': str(p), 'sha256': digest(p.read_bytes()),
                                'action_type': value.get('action_type'),
                                'planner_feedback': value.get('planner_feedback')})
    return {'candidates': candidates, 'actual_rejections': rejections,
            'consultant_actions': consult_actions,
            'ask_effect_compiler': results, 'compiler_source': str(FROZEN / 'agent_core/step_effects.py'),
            'compiler_scope': 'real compiler, recorded ask steps, empty capability registry; no model or full Planner replay',
            'provider_receipts': provider}


if __name__ == '__main__':
    main()
