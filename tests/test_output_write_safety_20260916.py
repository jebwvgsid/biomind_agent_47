"""Declared output protection; real graph/approval with an explicit process stub."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_core.action_runtime import (ActionRuntimeContractError, capture_output_write_guard,
    validate_output_write_guard, preserve_approved_output_files)


def contract(path):
    return [{'artifact_id': 'report', 'expected_path': str(path), 'artifact_kind': 'file'}]


@pytest.mark.parametrize('decision', ['approve', '', 'y'])
def test_existing_file_requires_explicit_backup_choice(tmp_path, decision):
    target = tmp_path / '中文 ann.vcf'; target.write_bytes(b'old result')
    guard = capture_output_write_guard(contract(target))
    with pytest.raises(ActionRuntimeContractError, match='OUTPUT_REPLACEMENT_NOT_APPROVED'):
        preserve_approved_output_files(guard, decision=decision, approval_identity={})
    assert target.read_bytes() == b'old result'
    assert not list(tmp_path.glob('.biomind-backup-*'))


def test_backup_is_independent_copy_and_exclusive(tmp_path):
    target = tmp_path / 'ann.vcf'; target.write_bytes(b'old result')
    guard = capture_output_write_guard(contract(target))
    receipt = preserve_approved_output_files(guard, decision='approve_backup_replace',
                                            approval_identity={'approval_identity_hash': 'approved'})
    backup = Path(receipt['backups'][0]['backup_path'])
    target.write_bytes(b'new result')
    assert backup.read_bytes() == b'old result'
    assert backup.stat().st_ino != target.stat().st_ino
    assert receipt['backups'][0]['sha256'] == hashlib.sha256(b'old result').hexdigest()
    # Neither a changed target nor a preexisting backup is ever overwritten.
    with pytest.raises(ActionRuntimeContractError):
        validate_output_write_guard(guard)


@pytest.mark.parametrize('change', ['appeared', 'changed', 'symlink', 'parent_symlink', 'scope', 'tampered'])
def test_target_changes_revoke_authority(tmp_path, change):
    target = tmp_path / 'ann.vcf'
    if change != 'appeared': target.write_text('old')
    guard = capture_output_write_guard(contract(target))
    if change in {'appeared', 'changed'}: target.write_text('different')
    elif change == 'symlink':
        target.unlink(); target.symlink_to(tmp_path / 'missing')
    elif change == 'parent_symlink':
        parent = tmp_path / 'dir'; parent.mkdir(); target = parent / 'ann.vcf'
        guard = capture_output_write_guard(contract(target))
        parent.rmdir(); parent.symlink_to(tmp_path, target_is_directory=True)
    elif change == 'tampered': guard['targets'] = []
    with pytest.raises(ActionRuntimeContractError):
        validate_output_write_guard(guard, contract(tmp_path / 'other') if change == 'scope' else None)


def test_backup_collision_preserves_both_files(tmp_path):
    target = tmp_path / 'ann.vcf'; target.write_text('original')
    guard = capture_output_write_guard(contract(target))
    backup = Path(guard['targets'][0]['backup_path']); backup.write_text('unrelated')
    with pytest.raises(FileExistsError):
        preserve_approved_output_files(guard, decision='approve_backup_replace', approval_identity={})
    assert backup.read_text() == 'unrelated' and target.read_text() == 'original'


def test_backup_parent_race_does_not_write_through_symlink(tmp_path, monkeypatch):
    parent = tmp_path / 'outputs'; parent.mkdir()
    other = tmp_path / 'unapproved'; other.mkdir()
    target = parent / 'ann.vcf'; target.write_text('original')
    guard = capture_output_write_guard(contract(target))
    original_open = os.open
    switched = False
    def raced_open(path, flags, *args, **kwargs):
        nonlocal switched
        fd = original_open(path, flags, *args, **kwargs)
        if path == 'ann.vcf' and kwargs.get('dir_fd') is not None and not switched:
            switched = True
            parent.rename(tmp_path / 'moved')
            parent.symlink_to(other, target_is_directory=True)
        return fd
    monkeypatch.setattr(os, 'open', raced_open)
    with pytest.raises(ActionRuntimeContractError):
        preserve_approved_output_files(guard, decision='approve_backup_replace', approval_identity={})
    assert switched and not list(other.iterdir())
    assert (tmp_path / 'moved' / 'ann.vcf').read_text() == 'original'


@pytest.mark.parametrize('answer,expected', [('', 'approve_backup_replace'), ('y', 'approve_backup_replace'), ('backup', 'approve_backup_replace'), ('n', 'reject')])
def test_existing_review_cannot_auto_approve_or_accept_enter(tmp_path, answer, expected):
    import agent_core.middleware as middleware
    target = tmp_path / 'ann.vcf'; target.write_text('old')
    provenance = {'output_write_guard': capture_output_write_guard(contract(target))}
    answers = [answer, 'backup'] if answer in {'', 'y'} else [answer]
    with patch.object(middleware, 'interrupt', side_effect=answers) as ask:
        result = middleware.HITLMiddleware(auto_approve_safe=True).review_code('true', 'Bash', provenance=provenance)
    assert ask.call_count == len(answers) and result[0] == expected
    payload = ask.call_args.args[0]
    assert payload['output_conflicts'][0]['path'] == str(target)
    assert 'backup' in payload['msg'] and '已有文件' in payload['content']
    if len(answers) == 2:
        assert ask.call_args_list[0].args[0]['content'] == ask.call_args_list[1].args[0]['content']


def test_optional_output_is_protected_without_becoming_required(tmp_path):
    from agent_core.managed_artifacts import ManagedArtifactBinding, validate_concrete_action_artifacts
    target = tmp_path / 'optional.tsv'; target.write_text('old optional result')
    binding = ManagedArtifactBinding(artifact_id='optional', semantic_handle='output.optional',
        direction='output', format='tsv', path_policy='managed_exact', canonical_path=str(target),
        required_basename=target.name, required=False, artifact_kind='file')
    action, errors = validate_concrete_action_artifacts({'type': 'execute',
        'code': 'true', 'argv': ['bash', '-c', 'true'], 'output_guard_contracts': []},
        {'output.optional': binding})
    assert not errors
    assert action['controller_output_bindings'] == []
    assert action['output_guard_contracts'][0]['required'] is False
    guard = capture_output_write_guard(action['output_guard_contracts'])
    with pytest.raises(ActionRuntimeContractError, match='OUTPUT_REPLACEMENT_NOT_APPROVED'):
        preserve_approved_output_files(guard, decision='approve', approval_identity={})
    assert target.read_text() == 'old optional result'


