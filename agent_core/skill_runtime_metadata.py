"""Resolve persisted BioMind environment registrations; Pi owns Skill loading."""
import json
from pathlib import Path


def domain_metadata(skill_md, frontmatter):
    """Legacy SkillManager compatibility: aliases only, no runtime/body sidecar."""
    aliases = json.loads(Path(__file__).with_name('skill_aliases.json').read_text(encoding='utf-8'))
    path = Path(skill_md).resolve().parent
    for canonical, values in aliases.items():
        if path.as_posix().endswith('/' + canonical):
            return {'legacy_ids': list(values)}
    return {}


class SkillRuntimeRegistry:
    """Use existing local records without package installation or healthchecks."""
    def __init__(self, root):
        self.root = Path(root).resolve()

    def resolve(self, skill_id, *, loaded_skills=None):
        """Resolve host-loaded identities, never discover another Skill catalog.

        loaded_skills is supplied only over the authenticated host channel.
        Legacy callers can still address an exact directory or persisted alias;
        they cannot invent a native name or authorize a file by passing a query.
        """
        registry = self.root / '.skill_runtime_registry.json'
        records = json.loads(registry.read_text(encoding='utf-8')).get('skills', {}) if registry.is_file() else {}
        aliases = json.loads(Path(__file__).with_name('skill_aliases.json').read_text(encoding='utf-8'))
        query = str(skill_id).removeprefix('skill:').casefold()
        if loaded_skills is None:
            # Bounded metadata enumeration, no rglob/frontmatter parser or cache.
            names = set(records) | set(aliases) | {str(skill_id)}
            loaded_skills = [{'name': '', 'file_path': str(self.root / name / 'SKILL.md')}
                            for name in names if not Path(name).is_absolute() and '..' not in Path(name).parts]
        if not isinstance(loaded_skills, list):
            raise ValueError('SKILL_LOADED_IDENTITIES_INVALID')
        identities = {}
        for item in loaded_skills:
            if not isinstance(item, dict) or not isinstance(item.get('file_path'), str):
                raise ValueError('SKILL_LOADED_IDENTITIES_INVALID')
            logical = Path(item['file_path'])
            real = logical.resolve()
            if (not logical.is_absolute() or logical.name != 'SKILL.md'
                    or not logical.is_relative_to(self.root) or not real.is_relative_to(self.root)
                    or not real.is_file()):
                continue
            canonical = real.parent.relative_to(self.root).as_posix()
            names = [canonical, real.parent.name, *aliases.get(canonical, [])]
            native = str(item.get('name') or '').removeprefix('skill:')
            if native: names.append(native)
            row = identities.setdefault(str(real), {'skill_id': canonical, 'names': set(), 'native_names': set()})
            row['names'].update(str(name).casefold() for name in names)
            if native: row['native_names'].add(native.casefold())
        exact = [row for row in identities.values() if query in row['native_names']]
        matches = exact or [row for row in identities.values() if query in row['names']]
        if len(matches) != 1:
            return {'status': 'ambiguous' if matches else 'unknown', 'record': {},
                    'reason_code': 'SKILL_RUNTIME_ALIAS_AMBIGUOUS' if matches else 'SKILL_UNKNOWN',
                    'candidates': sorted(row['skill_id'] for row in matches)}
        identity = matches[0]
        hits = []
        for key, value in records.items():
            normalized = str(key).casefold()
            owners = [row for row in identities.values() if row['skill_id'].casefold() == normalized]
            owners = owners or [row for row in identities.values() if normalized in row['native_names']]
            owners = owners or [row for row in identities.values() if normalized in row['names']]
            if identity not in owners:
                continue
            if len(owners) > 1:
                return {'status': 'ambiguous', 'skill_id': identity['skill_id'], 'record': {},
                        'reason_code': 'SKILL_RUNTIME_REGISTRATION_AMBIGUOUS', 'candidates': [str(key)]}
            hits.append((key, value))
        unique = {json.dumps(value, sort_keys=True) for _, value in hits}
        if len(unique) > 1:
            return {'status': 'ambiguous', 'skill_id': identity['skill_id'], 'record': {},
                    'reason_code': 'SKILL_RUNTIME_REGISTRATION_AMBIGUOUS', 'candidates': [key for key, _ in hits]}
        return {'status': 'registered' if hits else 'unregistered', 'skill_id': identity['skill_id'],
                'record': dict(hits[0][1]) if hits else {}, 'registration_keys': [key for key, _ in hits],
                'reason_code': 'SKILL_RUNTIME_REGISTERED' if hits else 'SKILL_RUNTIME_NOT_REGISTERED'}

    def get_runtime_registration(self, skill_id, *, loaded_skills=None):
        resolved = self.resolve(skill_id, loaded_skills=loaded_skills)
        if resolved['status'] == 'ambiguous':
            raise ValueError(resolved['reason_code'])
        return resolved['record']
