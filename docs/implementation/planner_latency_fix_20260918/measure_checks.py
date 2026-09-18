"""Opt-in replay of eight reviewed local read-only checks. No model or workflow.

Uses an existing Python environment; never installs dependencies. Output must be
new, so neither archived session state nor previous measurements are overwritten.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import types


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('before', 'after'), required=True)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.environ['BIOMIND_DISABLE_DOTENV'] = '1'
    here = Path(__file__).resolve().parent
    repo = here.parents[2]
    sys.path.insert(0, str(repo))
    project = args.project_root.resolve(strict=True)
    # Archived scripts contain this host's literal data/runtime locations.
    # Do not interpolate an arbitrary path into shell syntax or imply portability.
    if project != Path('/public/home/liusiqi/03.毕设'):
        parser.error('These reviewed commands are pinned to the original project root.')
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    evidence = json.loads((here / 'measurements.json').read_text())
    if args.mode == 'before':
        source = subprocess.check_output(['git', 'show', evidence['baseline_commit'] +
            ':agent_core/command_execution.py'], cwd=repo, text=True)
    else:
        source = (repo / 'agent_core/command_execution.py').read_text()
    module = types.ModuleType('measured_command')
    module.__file__ = str(repo / 'agent_core/command_execution.py')
    sys.modules[module.__name__] = module
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    svc = module.CommandExecutionService(data_path=output / 'receipts', project_dir=project)
    input_dir = project / 'benchmarks/01.测试/04.重测序/Part1.variant_calling/data'
    medium = project / 'benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-WGS-005/medium'
    scopes = [[str(medium.parent)], [str(input_dir)], [str(medium)], [], [str(input_dir)], [str(input_dir)], [], []]
    current = {}
    if args.mode == 'before':
        original_scan, original_prepare, original_execute = module._private_paths, svc._argv, svc._execute
        def scan(roots, **kwargs):
            roots = list(roots); start = time.monotonic()
            current.setdefault('scans', []).extend({'root': str(root), 'recursive': kwargs.get('recursive', True)} for root in roots)
            try:
                yield from original_scan(roots, **kwargs)
            finally:
                current['scan_seconds'] = current.get('scan_seconds', 0) + time.monotonic() - start
        def prepare(**kwargs):
            start = time.monotonic()
            try:
                return original_prepare(**kwargs)
            finally:
                current['prepare_seconds'] = time.monotonic() - start
        def execute(*values):
            start = time.monotonic()
            try:
                return original_execute(*values)
            finally:
                current['command_seconds'] = time.monotonic() - start
        module._private_paths, svc._argv, svc._execute = scan, prepare, execute
    async def replay():
        async def wake():
            while True:
                await asyncio.sleep(.02)
        ticker = asyncio.create_task(wake())
        results = []
        try:
            checks = [row for row in evidence['checks'] if row['mode'] == 'before']
            for index, check in enumerate(checks):
                current.clear()
                arguments = {'cmd': check['cmd'], 'timeout_ms': 120000}
                if args.mode == 'after':
                    arguments['read_paths'] = scopes[index]
                result = await svc.run(arguments, task_id='offline_measurement', stage='planner')
                results.append({'check': index + 1, 'receipt': result, 'instrumented': dict(current)})
                (output / 'results.json').write_text(json.dumps({
                    'mode': args.mode, 'cache': 'uncontrolled; no cache clearing',
                    'results': results}, ensure_ascii=False, indent=2))
                print(index + 1, result['status'], round(result['duration_seconds'], 3), flush=True)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(replay())
    finally:
        loop.close()


if __name__ == '__main__':
    main()
