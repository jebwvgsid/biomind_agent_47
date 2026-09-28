"""Scoped editable candidates using existing file receipts and task state.

The model sends content once, then exact patches and a submission action.
Validation consumes the complete current file. Acceptance here is candidate
validation only: existing PlanSnapshot/pending_action admission remains owner
of the immutable downstream handoff.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import re

from agent_core.workspace_files import WorkspaceFileService, _schema
from agent_core.state_contract_store import StateContractStore

DRAFT_TOOL_SCHEMAS = [
    _schema('create_draft_file', 'Create this task\'s controlled script or text draft once. Does not execute. Never overwrites an existing file.',
            {'name': {'type': 'string'}, 'content': {'type': 'string'}}, ['content']),
    _schema('read_draft_file', 'Read the current draft, or request contract=true for the current tool reference before creating a draft. For full replacement read every page using next_offset_chars and expected_sha256.',
            {'start_line': {'type': 'integer', 'minimum': 1}, 'max_lines': {'type': 'integer', 'minimum': 1, 'maximum': 400},
             'offset_chars': {'type': 'integer', 'minimum': 0}, 'max_chars': {'type': 'integer', 'minimum': 1, 'maximum': 6000},
             'expected_sha256': {'type': 'string'}, 'contract': {'type': 'boolean'}}, []),
    _schema('patch_draft_file', 'Edit only the registered current draft with its host-held version: use old_text/new_text for one exact span, OR content for whole-file replacement after reading the complete current version. Use whole-file replacement for Markdown/JSON conversion or trailing contamination. Does not submit or execute.',
            {'old_text': {'type': 'string'}, 'new_text': {'type': 'string'}, 'content': {'type': 'string'}}, []),
    _schema('submit_draft_file', 'Validate the complete saved draft. Fix all reported independent issues; read the complete file before changing its carrier/root structure. Do not weaken scientific requirements to satisfy a field error. Acceptance needs no full text reply.', {}, []),
]
DRAFT_TOOL_NAMES = frozenset(row['function']['name'] for row in DRAFT_TOOL_SCHEMAS)


def draft_response_for_history(message):
    """Keep host-loaded candidate bodies on disk, with a short provider receipt."""
    metadata = dict(getattr(message, 'response_metadata', None) or {})
    reference = metadata.get('draft_file') or {}
    if not isinstance(reference, dict) or not reference.get('path'):
        return message
    content = json.dumps({'draft_submission': {'path': reference['path'],
        'status': reference.get('status') or 'validated',
        'instruction': 'Continue through draft tools; complete content is read from disk by the validator.'}}, ensure_ascii=False)
    return message.model_copy(update={'content': content, 'id': None})


def _scope_from_state(state, stage):
    task = state.get('current_task') or {}
    current = state.get('current_plan') or {}
    scope = {'task_id': str(state.get('task_id') or ''),
             'task_revision': int(task.get('task_revision') or state.get('task_revision') or 0)}
    if stage == 'generate':
        from agent_core.plan_tracker import current_plan_step_with_index
        _, step = current_plan_step_with_index(state)
        scope.update(plan_id=str(state.get('plan_id') or current.get('plan_id') or ''),
                     plan_revision=int(state.get('plan_revision') or current.get('plan_revision') or 0),
                     plan_version=str(state.get('plan_version') or current.get('plan_version') or ''),
                     step_id=str(step.get('step_id') or ''))
    else:
        scope['planning_epoch_id'] = str(((state.get('task_state') or {}).get('planning_epoch') or {}).get('planning_epoch_id') or '')
    return scope


class DraftFileSession:
    """One active draft reference in the owning node's existing durable state.

    load_state/persist_state must use the caller's existing CAS state path.
    validate_submit(content, reference) may be async and returns accepted/errors.
    Neither tool arguments nor model messages carry a hash/binding registry.
    """
    def __init__(self, workspace_files, *, task_id, stage, load_state, persist_state,
                 validate_submit, scope=None, directory=None, default_name=None, read_sources=None):
        if stage not in {'planner', 'generate'}:
            raise ValueError('DRAFT_STAGE_INVALID')
        self.task_id, self.stage = task_id, stage
        self.load_state, self.persist_state = load_state, persist_state
        self.validate_submit = validate_submit
        self.scope = deepcopy(scope or _scope_from_state(load_state(), stage))
        self.default_name = default_name or ('plan.draft.json' if stage == 'planner' else '01.step.sh')
        commands = workspace_files.commands
        self.directory = Path(directory or commands._task_dir(task_id).parent).absolute()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.read_sources = dict(read_sources or {})
        self.files = WorkspaceFileService(commands, draft_root=self.directory, read_sources=self.read_sources)
        self.store = StateContractStore(commands.data_path, task_id)
        self.accepted_result = None
        self.accepted_content = None
        self.accepted_reference = None

    @property
    def schemas(self):
        schemas = deepcopy(DRAFT_TOOL_SCHEMAS)
        from agent_core.workspace_files import READ_FILE_SCHEMA
        schemas.append(deepcopy(READ_FILE_SCHEMA))
        if self.stage == 'planner':
            from agent_core.plan_contract import plan_admission_policy_enabled
            intent = ((self.load_state().get('task_state') or {}).get('intent_admission') or {}).get('request_kind')
            schemas[0]['function']['description'] = (
                'Create the requested plan document (Markdown is allowed). Saving/submitting this document does not execute it.'
                if intent == 'plan' else
                'Create one executable JSON Plan draft. Read read_draft_file(contract=true) for its authoritative minimal example. The filename extension does not select execution format.') + ' Never overwrites an existing file.'
            if intent != 'plan' and not plan_admission_policy_enabled():
                schemas[3]['function']['description'] = (
                    'Submit the complete saved JSON Plan. Plan content/schema policy screening is currently detached; '
                    'the host retains parseable steps/dependencies, current scope, file version and path authorization. '
                    'Submission does not execute work or establish that scientific requirements are met. '
                    'Keep scientific requirements intact; acceptance needs no full text reply.')
        schemas[0]['function']['description'] += f' Current output directory: {self.directory}; default name: {self.default_name}.'
        return schemas

    @property
    def state_key(self):
        return 'planner_loop' if self.stage == 'planner' else 'generate_context_episode'

    @property
    def reference(self):
        return deepcopy(((self.load_state().get('task_state') or {}).get(self.state_key) or {}).get('draft_file') or {})

    def _save(self, record, *, expected=None):
        state = deepcopy(self.load_state())
        prior = deepcopy(((state.get('task_state') or {}).get(self.state_key) or {}).get('draft_file') or {})
        if expected is not None and prior != expected:
            raise ValueError('DRAFT_STATE_CHANGED: reload the current draft before continuing')
        record = deepcopy(record)
        record['record_ref'] = self.store.put_object('draft_file_state', {k: v for k, v in record.items() if k != 'record_ref'})
        task_state = state.setdefault('task_state', {})
        owner_state = task_state.setdefault(self.state_key, {})
        if self.stage == 'planner' and not owner_state.get('schema_version'):
            from agent_core.planning_state_facts import new_planner_loop_state
            owner_state = {**new_planner_loop_state(self.task_id), **owner_state}
            task_state[self.state_key] = owner_state
        if prior and prior.get('scope') != record.get('scope'):
            owner_state.setdefault('draft_scope_history', []).append(deepcopy(prior))
        owner_state['draft_file'] = record
        self.persist_state(state)
        return record

    def _current(self):
        ref = self.reference
        if not ref or not ref.get('path'):
            raise ValueError('DRAFT_MISSING: create_draft_file first')
        if ref.get('scope') != self.scope:
            raise ValueError('DRAFT_SCOPE_CHANGED: current task or Plan changed; create a new named draft')
        return ref

    def _live_scope_matches(self):
        state = self.load_state()
        live = _scope_from_state(state, self.stage)
        if 'user_turn_revision' in self.scope or 'request_key' in self.scope:
            from agent_core.request_state import request_scope
            live.update(request_scope(state))
        return all(live.get(key) == value for key, value in self.scope.items() if key in live)

    @staticmethod
    def _brief(ref):
        return {key: deepcopy(ref[key]) for key in ('path', 'sha256', 'bytes', 'status', 'validation_issues', 'receipt_ref') if key in ref}

    def _plan_display(self, record, content):
        if self.stage != 'planner' or str(record.get('path', '')).endswith('.md'):
            return record
        try:
            plan = json.loads(content)
        except ValueError:
            plan = {}
        lines = ['# Plan', '', 'Status: editable work plan; execution requires an explicit command.', '',
                 'Editable source: `'+Path(record['path']).name+'`', '']
        if isinstance(plan, dict):
            for index, step in enumerate(plan.get('steps') or [], 1):
                if isinstance(step, dict):
                    label = step.get('title') or step.get('name') or step.get('description') or step.get('step_id') or str(index)
                    lines.append(f'{index}. {label}')
        display = self.directory / 'plan.md'
        expected = record.get('display_sha256', '')
        try:
            receipt = self.files.write({'path': str(display), 'content': '\n'.join(lines)+'\n', 'expected_sha256': expected}, task_id=self.task_id)
            record['display_sha256'] = receipt['sha256']
            record['display_path'] = str(display)
            record.pop('display_warning', None)
        except (ValueError, OSError) as exc:
            record['display_warning'] = str(exc)  # Never overwrite a foreign user file.
        return record

    def record_rejection(self, issues):
        """Outer existing validators retain correction facts on the same draft."""
        ref = self._current()
        previous = deepcopy(ref)
        ref.update(status='rejected', validation_issues=deepcopy(list(issues)))
        self.accepted_result = self.accepted_content = self.accepted_reference = None
        self._save(ref, expected=previous)

    def restore_missing_from_content(self):
        """Explicit host recovery, never automatic at open/read/create.

        Only the active reference's exact immutable bytes may be recovered.
        This method does not alter a Plan snapshot, revision, or Commit.
        """
        if not self._live_scope_matches():
            raise ValueError('DRAFT_SCOPE_CHANGED')
        ref = self._current()
        old_path = Path(ref['path'])
        if old_path.exists() or old_path.is_symlink():
            raise ValueError('DRAFT_RECOVERY_REQUIRES_MISSING_FILE')
        if not ref.get('content_ref'):
            raise ValueError('DRAFT_MATCHING_CONTENT_UNAVAILABLE: last edits must not be rolled back')
        content = self.store.read_object(ref['content_ref'], expected_type='draft_candidate_content')
        body = content.get('content')
        if (not isinstance(body, str) or content.get('scope') != ref.get('scope')
                or content.get('sha256') != ref['sha256']
                or hashlib.sha256(body.encode('utf-8')).hexdigest() != ref['sha256']):
            raise ValueError('DRAFT_RECOVERY_VERSION_MISMATCH')
        directory = self.files.commands._task_dir(self.task_id).parent
        files = WorkspaceFileService(self.files.commands, draft_root=directory, read_sources=self.read_sources)
        receipt = self._create_named(files, old_path.name, body, internal=True)
        record = {**ref, **receipt, 'status': ref['status'], 'recovered_from': ref['path']}
        try:
            saved = self._save(record, expected=ref)
        except (OSError, ValueError) as exc:
            return {'status': 'not_run', 'error': str(exc), 'registration_state': 'not_accepted',
                    'candidate_file': self._brief(record), 'retained': True}
        self.directory, self.files = directory, files
        return self._brief(saved)

    def _create_named(self, files, filename, content, *, internal):
        for ordinal in range(32 if internal else 1):
            candidate = filename if ordinal == 0 else Path(filename).stem + '.' + str(ordinal) + Path(filename).suffix
            try:
                return files.write({'path': candidate, 'content': content, 'expected_sha256': ''}, task_id=self.task_id)
            except ValueError as exc:
                if not internal or not str(exc).startswith('FILE_CHANGED:'):
                    raise
        raise ValueError('DRAFT_NAME_COLLISIONS_EXHAUSTED')

    async def dispatch(self, name, args, *, task_id=None):
        try:
            if task_id is not None and task_id != self.task_id:
                raise ValueError('DRAFT_TASK_MISMATCH')
            if not isinstance(args, dict):
                raise ValueError('DRAFT_ARGUMENTS_MUST_BE_OBJECT')
            if not self._live_scope_matches():
                raise ValueError('DRAFT_SCOPE_CHANGED: task or approved Plan changed during editing')
            if name == 'read_draft_file' and args.get('contract') is True:
                from agent_core.work_plan import plan_reference as executable_plan_reference
                return {'status': 'completed', 'reference': 'lightweight_plan_v4',
                        'content': json.dumps(executable_plan_reference(), ensure_ascii=False), 'complete': True}
            if name == 'read_file':
                # Reading an unregistered file never adopts it as a mutable draft.
                path = Path(args.get('path') or '')
                service = self.files if path.is_absolute() and path.is_relative_to(self.directory) else WorkspaceFileService(self.files.commands, read_sources=self.read_sources)
                return service.read(args, task_id=self.task_id)
            if name == 'create_draft_file':
                filename = args.get('name') or self.default_name
                if not isinstance(filename, str) or not re.fullmatch(r'[\w.-]{1,160}', filename) or filename in {'.', '..'}:
                    raise ValueError('DRAFT_BASENAME_REQUIRED')
                if self.stage == 'planner' and not filename.endswith(('.json', '.md')):
                    raise ValueError('PLAN_DRAFT_JSON_OR_MARKDOWN_REQUIRED')
                previous = self.reference
                if previous and previous.get('scope') == self.scope:
                    raise ValueError('DRAFT_ALREADY_EXISTS: read and patch the existing draft')
                internal = ((self.load_state().get('task_state') or {}).get('intent_admission') or {}).get('request_kind') == 'execute'
                receipt = self._create_named(self.files, filename, args.get('content'), internal=internal)
                record = {**receipt, 'scope': deepcopy(self.scope), 'stage': self.stage, 'status': 'editing', 'validation_issues': []}
                if previous.get('display_path') == str(self.directory / 'plan.md'):
                    record['display_sha256'] = previous.get('display_sha256', '')
                record = self._plan_display(record, args['content'])
                try:
                    return self._brief(self._save(record, expected=previous))
                except (ValueError, OSError) as exc:
                    return {'status': 'not_run', 'error': str(exc), 'registration_state': 'not_accepted',
                            'candidate_file': self._brief(record), 'retained': True,
                            'instruction': 'Reload the active draft reference; the retained candidate is not the current draft.'}
            if name not in DRAFT_TOOL_NAMES:
                raise ValueError('UNKNOWN_DRAFT_TOOL')
            ref = self._current()
            previous = deepcopy(ref)
            if name == 'read_draft_file':
                result = self.files.read({'path': ref['path'], **{key: value for key, value in args.items() if key in {'start_line', 'max_lines', 'offset_chars', 'max_chars', 'expected_sha256'}}}, task_id=self.task_id)
                if result['sha256'] != ref['sha256']:
                    ref.update(status='editing', validation_issues=[])
                    ref.pop('content_ref', None)
                    self.accepted_result = self.accepted_content = self.accepted_reference = None
                ref.update(sha256=result['sha256'], bytes=result['bytes'])
                coverage = ref.get('read_coverage') or {}
                end = int(coverage.get('end_char') or 0) if coverage.get('sha256') == result['sha256'] else 0
                start = result['offset_chars']
                if start <= end:
                    end = max(end, start + len(result['content']))
                ref['read_coverage'] = {'sha256':result['sha256'], 'end_char':end,
                                        'complete':end == result['total_chars']}
                result['full_draft_read'] = ref['read_coverage']['complete']
                self._save(ref, expected=previous)
                return result
            if name == 'patch_draft_file':
                if 'content' in args:
                    if 'old_text' in args or 'new_text' in args:
                        raise ValueError('DRAFT_PATCH_MODE_CONFLICT: choose content OR old_text/new_text')
                    coverage = ref.get('read_coverage') or {}
                    if coverage.get('sha256') != ref['sha256'] or not coverage.get('complete'):
                        raise ValueError('DRAFT_FULL_READ_REQUIRED: read the complete current draft before whole-file replacement')
                    receipt = self.files.write({'path':ref['path'], 'expected_sha256':ref['sha256'], 'content':args['content']}, task_id=self.task_id)
                else:
                    receipt = self.files.patch({'path': ref['path'], 'expected_sha256': ref['sha256'], 'old_text': args.get('old_text'), 'new_text': args.get('new_text')}, task_id=self.task_id)
                ref.update(receipt)
                ref.update(status='editing', validation_issues=[])
                ref.pop('content_ref', None)
                ref.pop('read_coverage', None)
                current = self.files.read_complete(ref['path'], task_id=self.task_id)
                ref = self._plan_display(ref, current['content'])
                self.accepted_result = self.accepted_content = self.accepted_reference = None
                return self._brief(self._save(ref, expected=previous))
            current = self.files.read_complete(ref['path'], task_id=self.task_id)
            self.accepted_result = self.accepted_content = self.accepted_reference = None
            if current['sha256'] != ref['sha256']:
                raise ValueError('FILE_CHANGED: read the current draft before submitting')
            result = self.validate_submit(current['content'], deepcopy(ref))
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, dict) or type(result.get('accepted')) is not bool:
                raise ValueError('DRAFT_VALIDATOR_RESULT_INVALID')
            # Validator may await I/O. Never accept a changed working file or scope.
            verified = self.files.read_complete(ref['path'], task_id=self.task_id)
            if verified['sha256'] != current['sha256'] or not self._live_scope_matches():
                raise ValueError('FILE_OR_SCOPE_CHANGED_DURING_VALIDATION')
            ref.update(status='validated' if result['accepted'] else 'rejected',
                       validation_issues=deepcopy(result.get('errors') or result.get('validation_issues') or []))
            if result.get('plan_admission_policy'):
                ref['plan_admission_policy'] = result['plan_admission_policy']
            if result['accepted']:
                # This immutable candidate object is not execution authorization.
                ref['content_ref'] = self.store.put_object('draft_candidate_content', {'content': current['content'], 'path': ref['path'], 'sha256': current['sha256'], 'scope': self.scope})
            ref = self._save(ref, expected=previous)
            if result['accepted']:
                self.accepted_result = deepcopy(result)
                self.accepted_content = current['content']
                self.accepted_reference = deepcopy(ref)
            return {'accepted': result['accepted'], **self._brief(ref), 'issues':deepcopy(ref.get('validation_issues') or []),
                **({'plan_admission_policy':result['plan_admission_policy']} if result.get('plan_admission_policy') else {}),
                'syntax_check': deepcopy(result.get('syntax_check')), 'not_evaluated':deepcopy(result.get('not_evaluated') or []),
                'execution_started_by_submission': False,
                'message': 'Saved and checked only. Existing execution records are unchanged.'}
        except (OSError, ValueError) as exc:
            result = {'status': 'not_run', 'error': str(exc), 'error_type': type(exc).__name__}
            if name == 'create_draft_file' and 'FILE_CHANGED' in str(exc):
                result.update(disk_state='existing_unregistered', next_actions=['read_file','create_draft_file'],
                    instruction='Read the existing file if needed, then create a differently named draft. Reading does not authorize overwrite or adoption.')
            elif 'DRAFT_MISSING' in str(exc):
                result.update(registration_state='missing', next_actions=['read_file','create_draft_file'])
            elif isinstance(exc, FileNotFoundError):
                result.update(disk_state='missing', registration_state='present' if self.reference else 'missing',
                    instruction='Active draft file is missing. The formal Plan is separate and unchanged; recover only matching draft bytes with explicit authorization, never silently restore an older submitted version.')
            return result
