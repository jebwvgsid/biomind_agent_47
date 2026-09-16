"""Export current safe bytes and run witnesses with the existing A1 guards.

This host script imports test export/launch utilities, never BioMind itself.
No configuration, credentials, historical scripts or scientific inputs exported.
"""
from pathlib import Path
import argparse
import ast
import hashlib
import importlib.util
import json
import shutil
import sys

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / "bio_agents.py").is_file())


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--results", default="results")
    parser.add_argument("--filter", default="")
    parser.add_argument("targets", nargs="*")
    args = parser.parse_args()
    root = args.root.resolve()
    assert root.is_relative_to(Path('/tmp')) and root != Path('/tmp')
    if args.prepare:
        assert not root.exists(), "Use a new isolated export; never overwrite an old witness."
        exporter = load('phase_d_export', REPO / 'tests/a1_unified_fix/export_and_run.py')
        dependencies = {
            'tests/test_inspection_workflow.py', 'tests/test_recovery_projection.py',
            'tests/phase_b_1789471955/test_integration.py',
            'tests/phase_b_1789471955/test_control.py',
            'tests/test_observation_warning_recovery_stop.py',
        }
        for source in HERE.glob('test_*.py'):
            for node in ast.walk(ast.parse(source.read_text())):
                modules = ([node.module] if isinstance(node, ast.ImportFrom) and node.module else
                           [a.name for a in node.names] if isinstance(node, ast.Import) else [])
                for module in modules:
                    if module.startswith('tests.'):
                        path = module.replace('.', '/') + '.py'
                        if (REPO / path).is_file():
                            dependencies.add(path)
        manifest = exporter.export(root / 'export', sorted(dependencies))
        destination = root / 'export/tests/phase_d_1789521763'
        destination.mkdir()
        for source in sorted(HERE.glob('test_*.py')):
            target = destination / source.name
            shutil.copyfile(source, target)
            manifest['files'][str(target.relative_to(root / 'export'))] = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest['phase_d_witness_origin'] = str(HERE.relative_to(REPO))
        (root / 'export/export_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'export': str(root / 'export'), 'source_signature': manifest['source_signature'],
                          'witnesses': sorted(p.name for p in destination.glob('test_*.py'))}))
    else:
        launcher = load('phase_d_launch', REPO / 'tests/cli_model_progress_support.py')
        targets = args.targets or ['tests/phase_d_1789521763']
        if args.filter:
            targets = ['-k', args.filter, *targets]
        code = launcher.launch(root / 'export', root / args.results,
                               ['--noconftest', '-q', '-p', 'no:cacheprovider', *targets],
                               installed_cli=True, watchdog_seconds=360)
        raise SystemExit(code)


if __name__ == '__main__':
    main()
