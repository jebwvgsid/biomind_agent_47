"""Check byte-exact P_test from the captured request and inspected pure fixture AST.

This script imports only the standard library. It neither imports production nor
loads an agent/session, performs scientific I/O, or writes existing evidence.
"""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace


def main():
    here = Path(__file__).resolve().parent
    repo = next(parent for parent in here.parents if (parent / 'tests/a1_unified_fix/test_f6_graph.py').is_file())
    source = repo / 'tests/a1_unified_fix/test_f6_graph.py'
    qualification = json.loads((here / 'P_test.reconstruction.json').read_text())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == qualification['source_sha256']
    module = ast.parse(source.read_text())
    cls = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == 'F6Graph')
    plan = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'plan')
    operations = next(ast.literal_eval(node.value) for node in module.body
                      if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'OPS' for t in node.targets))
    request = json.loads((here / 'P_test.planner_request.json').read_text())
    public_text = request['messages'][1]['content'].split('</phase_seed_contract>', 1)[1].lstrip()
    context, _ = json.JSONDecoder().raw_decode(public_text)
    capabilities = context['semantic_history']['capabilities'][0]['execution_capabilities']
    registry = {row['capability_id']: row for row in capabilities}
    ns = {'deepcopy': deepcopy, 'OPS': operations, 'SID': 'genomics/snpeff-annotation'}
    exec(compile(ast.Module(body=[plan], type_ignores=[]), str(source), 'exec'), ns)
    owner = SimpleNamespace(mode='normal', agent=SimpleNamespace(skill_manager=SimpleNamespace(
        get_execution_capability_registry=lambda selected: registry)))
    raw = json.dumps(ns['plan'](owner)).encode()
    assert raw == (here / 'P_test.raw.json').read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    assert checksum == json.loads((here / 'P_test.original_claim.json').read_text())['raw_digest']
    print(json.dumps({'result': 'MATCH', 'bytes': len(raw), 'sha256': checksum,
                      'method': 'only inspected fixture.plan AST + actually delivered public capabilities'}))


if __name__ == '__main__':
    main()
