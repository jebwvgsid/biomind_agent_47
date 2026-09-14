"""Real public Skill catalog construction in the guarded, inert-config export."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

def test_current_registry_and_loaded_bytes():
    from agent_core.skill_manager import SkillManager
    from agent_core.workflow import runtime_graph, outcomes
    from agent_core import step_effects, plan_contract, candidate_repair_policy
    from prompts import manager
    root = Path(__file__).resolve().parents[2]
    sm = SkillManager(str(root / 'skills'))
    ids = [s['_skill_id'] for s in sm.discovered_skills]
    # The public task selection API deliberately caps one selection at 12.
    # Inventory each registered Skill through that same API, rather than treating
    # the entire catalog as one synthetic task selection.
    registry = {}
    for skill_id in ids:
        selected = sm.get_execution_capability_registry([skill_id])
        assert not set(selected).intersection(registry), skill_id
        registry.update(selected)
    rows = []
    for capability, declaration in sorted(registry.items()):
        rows.append({'capability': capability, 'declaration': declaration})
    modules = {}
    dependencies = {}
    for name, module in list(sys.modules.items()):
        raw = getattr(module, '__file__', None)
        if raw and Path(raw).is_relative_to(root) and Path(raw).is_file():
            modules[name] = {'file': raw, 'sha256': hashlib.sha256(Path(raw).read_bytes()).hexdigest()}
        elif raw and name in {'pytest', 'yaml', 'pydantic', 'langchain_core', 'langgraph.graph',
                              'langgraph.checkpoint.memory', 'prompt_toolkit'} and Path(raw).is_file():
            dependencies[name] = {'file': raw, 'sha256': hashlib.sha256(Path(raw).read_bytes()).hexdigest()}
    result = {'catalog': sm.catalog_status(), 'skill_count': len(ids), 'capability_count': len(rows),
              'skills': [{'id': s['_skill_id'], 'path': s['_skill_md'], 'sha256': s['_skill_content_hash'],
                          'scripts': s.get('_scripts', []), 'references': s.get('_references', [])}
                         for s in sm.discovered_skills],
              'capabilities': rows, 'loaded_modules': modules,
              'loaded_dependency_entrypoints': dependencies,
              'installed_distribution_versions': {name: importlib.metadata.version(name)
                for name in ('pytest', 'PyYAML', 'pydantic', 'langchain-core', 'langgraph', 'prompt-toolkit')},
              'typed_outcome_branches': {name: sorted(cls.ALLOWED_TYPES)
                for name, cls in vars(outcomes).items()
                if isinstance(cls, type) and getattr(cls, 'ALLOWED_TYPES', None)},
              'runtime_registry': 'rebuilt from public Skill declarations; host installation registry not loaded',
              'real_provider_calls': 0, 'scientific_processes': 0,
              'coverage': 'CURRENT_BEHAVIOR_VERIFIED: actual SkillManager declaration consumer; no capability availability claim'}
    out = Path(os.environ['BIOMIND_GER_ARTIFACTS']) / 'registry_inventory.json'
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + '\n')
    assert sm.catalog_complete, sm.catalog_errors
    assert 'genomics.snpeff_annotation.check_contig_compatibility' in registry
    assert all(s['_skill_content_hash'] == hashlib.sha256(Path(s['_skill_md']).read_bytes()).hexdigest()
               for s in sm.discovered_skills)
