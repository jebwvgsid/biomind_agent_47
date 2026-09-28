"""New tiny inputs + real batch/executor and production Recovery/Generate.

No original session or original script is an executable fixture. Run only via
the established safe export, two audit guards and persistent fatal outlet.
"""
import asyncio
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from execution.executor import run_command
from agent_core.step_effects import extract_structured_effect_evidence
from agent_core.cli_display import bind_terminal
from tests.cli_model_progress_support import TimedWriter

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / 'skills/genomics/wgs-short-read-mapping/scripts/inspect_fastq_batch.py'
PYTHON = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python'
ENV = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}


def save(name, value):
    root = Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS'])
    (root / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def digest_tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}


async def approved_tiny(path, code):
    path.write_text('#!/bin/bash\n' + code + '\n')
    sys.modules['__main__'].OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path))
    return await run_command('bash ' + shlex.quote(str(path)), timeout=20, profile=False, env=ENV)


def batch_args(directory, paths, *, joined=False, observation='audit-prefix'):
    argv = [PYTHON, str(BATCH), '--observation-id', observation,
            '--evidence-dir', str(directory), '--item-timeout-seconds', '5']
    for label, path in paths:
        argv += ['--item', label + ' ' + str(path)] if joined else ['--item', label, str(path)]
    return argv


def test_real_batch_parameter_directory_and_fasta_counterexamples(tmp_path):
    async def scenario():
        inputs = []
        for i in range(4):
            p = tmp_path / ('input %d.fq.gz' % i)
            p.write_bytes(gzip.compress(b'@synthetic\nACGT\n+\nIIII\n', mtime=0))
            inputs.append(('S%d' % i, p))
        fasta = tmp_path / 'synthetic-long.fasta'
        fasta.write_text('>synthetic\n' + 'A' * 17000 + '\n')
        short = tmp_path / 'synthetic-short.fasta'; short.write_text('>synthetic\nACGT\n')
        cases = [
            ('joined_item', True, True, inputs + [('reference_fasta', fasta)], 2, 'expected 2 arguments'),
            ('precreated_leaf', False, True, inputs, 1, 'FileExistsError'),
            ('renamed_but_precreated', False, True, inputs, 1, 'FileExistsError'),
            ('old_leaf_without_mkdir', False, False, inputs, 1, 'FileExistsError'),
            ('new_leaf', False, False, inputs, 0, None),
            ('next_attempt_new_leaf', False, False, inputs, 0, None),
            ('third_error_long_fasta', False, False, inputs + [('reference_fasta', fasta)], 2, None),
            ('short_fasta_is_not_fastq', False, False, inputs + [('reference_fasta', short)], 0, None),
        ]
        previous = None; old_evidence = {}; results = []
        for index, (name, joined, mkdir, paths, expected, error) in enumerate(cases):
            leaf = tmp_path / name
            if name == 'old_leaf_without_mkdir':
                leaf = tmp_path / 'precreated_leaf'
            argv = batch_args(leaf, paths, joined=joined)
            code = ('mkdir -p ' + shlex.quote(str(leaf)) + '\n' if mkdir else '') + 'exec ' + shlex.join(argv)
            out, err, rc = await approved_tiny(tmp_path / (name + '.sh'), code)
            save('batch-attempt-' + name, {'argv': argv, 'code': code, 'stdout': out, 'stderr': err, 'returncode': rc})
            assert rc == expected, (name, rc, err)
            if error: assert error in err
            observations = extract_structured_effect_evidence(out).get('produces_observations', [])
            row = {'case': name, 'argv': argv, 'returncode': rc, 'stdout': out, 'stderr': err,
                   'observation_count': len(observations), 'model_calls': 0}
            if index < 4:
                assert not observations and not (leaf / 'checks.json').exists()
                assert 'BIOMIND_PREFLIGHT_PROGRESS_JSON=' not in err
            else:
                assert len(observations) == 1 and observations[0]['identity'] == 'audit-prefix'
                value = observations[0]['value']; row['value'] = value
                checks = json.loads((leaf / 'checks.json').read_text()); row['checks'] = checks
                assert value['full_file_validated'] is False and value['passed'] == 4
                if name == 'third_error_long_fasta':
                    assert value['status'] == 'incomplete' and value['returned'] == 5
                    last = value['checks'][-1]; assert last['returncode'] == 2
                    raw = Path(last['raw']['stderr']).read_text(); row['nested_stderr'] = raw
                    assert json.loads(raw)['error']['code'] == 'READ_LINE_LIMIT'
                elif name == 'short_fasta_is_not_fastq':
                    assert value['status'] == 'returned' and not value['checks'][-1]['passed']
                else:
                    assert value['status'] == 'returned' and value['returned'] == 4
                    if previous: assert digest_tree(previous) == old_evidence
                    previous = leaf; old_evidence = digest_tree(leaf)
            results.append(row)
        save('batch-counterexamples', {'cases': results, 'wrapper_calls': len(cases),
             'nested_reads': 18, 'real_models': 0, 'old_evidence_preserved': True,
             'scope': 'FASTQ prefixes only; new tiny synthetic files, never original inputs'})
    asyncio.run(scenario())


