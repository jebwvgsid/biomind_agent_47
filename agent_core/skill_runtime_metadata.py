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

    def get_runtime_registration(self, skill_id):
        registry = self.root / '.skill_runtime_registry.json'
        records = json.loads(registry.read_text(encoding='utf-8')).get('skills', {}) if registry.is_file() else {}
        aliases = json.loads(Path(__file__).with_name('skill_aliases.json').read_text(encoding='utf-8'))
        matches = []
        for skill_md in sorted(self.root.rglob('SKILL.md')):
            if not skill_md.resolve().is_relative_to(self.root):
                continue
            canonical = skill_md.parent.relative_to(self.root).as_posix()
            candidates = [canonical, skill_md.parent.name, *aliases.get(canonical, [])]
            if str(skill_id).casefold() in {str(x).casefold() for x in candidates}:
                matches.append(candidates)
        if len(matches) > 1:
            raise ValueError('SKILL_RUNTIME_ALIAS_AMBIGUOUS')
        for key in matches[0] if matches else [skill_id]:
            if key in records:
                return dict(records[key])
        return {}
