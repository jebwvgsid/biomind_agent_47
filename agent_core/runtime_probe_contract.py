"""Fixed argv help/version probes shared by preflight and bounded discovery.

Skill metadata selects read-only probe operations; it cannot supply shell code.
The result states which exact launcher was checked, independently for each fact.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import signal
from pathlib import Path
from typing import Mapping

from agent_core.action_runtime import _runtime_binding_identity

# This literal is the only Python program admitted by the inspection sandbox.
# Regular files avoid a descendant retaining a PIPE after its parent exits.
PROBE_DRIVER = r'''
import json, os, signal, subprocess, sys, tempfile, time
def stop(signum, frame):
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, stop)
deadline = time.monotonic() + 50
spec = json.loads(sys.argv[1])
facts = {}
for probe in spec['probes']:
    ok = True
    reason = ''
    for check in probe['checks']:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise subprocess.TimeoutExpired(check['argv'], 0)
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen(check['argv'], stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, env=spec['environment'],
                    cwd='/tmp', close_fds=True, start_new_session=True)
                try:
                    code = process.wait(timeout=min(check['timeout'], remaining))
                finally:
                    # A launcher may leave a Java child behind, including on a
                    # nominally successful exit. Retire the complete probe group.
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    process.wait()
                output.seek(0)
                text = output.read(65537).decode('utf-8', errors='replace')
            ok = code in check['success_codes'] and all(token in text for token in check['contains']) and len(text.encode()) <= 65536
            reason = '' if ok else 'RUNTIME_PROBE_COMMAND_MISMATCH'
        except (OSError, subprocess.TimeoutExpired):
            ok, reason = False, 'RUNTIME_PROBE_EXECUTION_INCOMPLETE'
        if not ok: break
    facts[probe['output']] = {'ok': ok, 'reason_code': reason}
    if ok:
        print(probe['output'] + '=' + probe['value'])
print('BIOMIND_PROBE_RESULTS=' + json.dumps(facts, sort_keys=True))
'''.strip()


def probe_method(requirements: Mapping) -> str:
    return json.dumps(requirements.get("inspection_probes") or [], sort_keys=True, separators=(",", ":"))


def build_probe_specification(requirements: Mapping, prefix: Path) -> dict:
    """Admit a small argv vocabulary, never arbitrary commands or shell flags."""
    prefix = prefix.resolve(strict=True)
    probes = []
    seen = set()
    for raw in requirements.get("inspection_probes") or []:
        name = str(raw.get("output") or "")
        if not re.fullmatch(r"RESOLVED_[A-Z][A-Z0-9_]+", name) or name in seen:
            raise ValueError("RUNTIME_PROBE_OUTPUT_INVALID")
        seen.add(name)

        def path(value):
            text = str(value).replace("{prefix}", str(prefix))
            p = Path(text)
            # Preserve the invocation path, including venv symlinks.
            if not p.is_absolute() or ".." in p.parts or not (p.is_relative_to(prefix) or text == "/usr/bin/python3"):
                raise ValueError("RUNTIME_PROBE_PATH_OUT_OF_SCOPE")
            return str(p)

        value = path(raw["value"])
        checks = []
        for check in raw.get("checks") or []:
            argv = [str(arg).replace("{prefix}", str(prefix)) for arg in check.get("argv") or []]
            if not argv or len(argv) > 8:
                raise ValueError("RUNTIME_PROBE_ARGV_INVALID")
            executable = path(argv[0])
            tool = Path(executable).name
            args = argv[1:]
            if tool == "python3" and args and Path(args[0]).name == "gatk":
                path(args.pop(0))
                tool = "gatk"
            # Only a declared tool's version/help or known no-argument usage
            # entry can run. In particular no Python code, output arguments,
            # input files, shell wrappers, install or mutating operations.
            admitted = (
                tool in {"bwa", "samtools"} and len(args) <= 2
                and all(arg in {"index", "mem", "faidx", "sort", "index", "flagstat", "depth", "quickcheck", "coverage", "dict", "--help", "--version"} for arg in args)
                and (not args or args[-1] in {"--help", "--version"} or tool == "bwa" and args in [["index"], ["mem"]] or tool == "samtools" and args == ["faidx"])
            ) or (tool in {"java", "python3", "bcftools"} and args in [["-version"], ["--version"], ["stats", "--help"]]) or (
                tool == "bcftools" and args == ["query", "--help"]
            ) or (
                tool == "picard" and args in [["CreateSequenceDictionary", "--help"], ["MarkDuplicates", "--help"]]
            ) or (tool == "gatk" and len(args) == 2 and args[0] in {
                "HaplotypeCaller", "CombineGVCFs", "GenotypeGVCFs", "SelectVariants", "VariantFiltration", "ValidateVariants"
            } and args[1] == "--help")
            if not admitted:
                raise ValueError("RUNTIME_PROBE_OPERATION_NOT_ADMITTED")
            codes = check.get("success_codes", [0])
            contains = check.get("contains") or []
            if not codes or any(type(code) is not int or code not in {0, 1, 255} for code in codes) or not contains or any(not isinstance(token, str) or not token or len(token) > 200 for token in contains):
                raise ValueError("RUNTIME_PROBE_EXPECTATION_INVALID")
            checks.append({"argv": argv, "success_codes": list(codes), "contains": list(contains), "timeout": 8})
        if not checks or len(checks) > 8:
            raise ValueError("RUNTIME_PROBE_CHECK_REQUIRED")
        probes.append({"output": name, "value": value, "checks": checks})
    if not probes or len(probes) > 16:
        raise ValueError("RUNTIME_PROBE_CONTRACT_INVALID")
    return {"probes": probes, "environment": {
        "PATH": str(prefix / "bin") + ":/usr/bin:/bin", "JAVA_HOME": str(prefix),
        "CONDA_PREFIX": str(prefix), "CONDA_DEFAULT_ENV": prefix.name,
        "LC_ALL": "C", "TMPDIR": "/tmp", "HOME": "/tmp",
    }}


def probe_argv(specification: Mapping) -> list[str]:
    return ["/usr/bin/python3", "-I", "-S", "-c", PROBE_DRIVER, json.dumps(specification, sort_keys=True)]


def parse_probe_result(stdout: str, specification: Mapping) -> dict:
    paths, coverage = {}, {}
    lines = stdout.splitlines()
    statuses = [line.partition("=")[2] for line in lines if line.startswith("BIOMIND_PROBE_RESULTS=")]
    if len(statuses) != 1:
        raise ValueError("RUNTIME_PROBE_RESULT_INVALID")
    outcomes = json.loads(statuses[0])
    expected = {row["output"] for row in specification["probes"]}
    if not isinstance(outcomes, dict) or set(outcomes) != expected or any(
        not isinstance(row, dict) or type(row.get("ok")) is not bool or not isinstance(row.get("reason_code"), str)
        for row in outcomes.values()
    ):
        raise ValueError("RUNTIME_PROBE_RESULT_INVALID")
    for probe in specification["probes"]:
        name = probe["output"]
        values = [line.partition("=")[2] for line in lines if line.startswith(name + "=")]
        ok = outcomes.get(name, {}).get("ok") is True and values == [probe["value"]]
        if ok:
            p = Path(probe["value"])
            ok = p.is_file() and os.access(p, os.X_OK)
        if ok:
            paths[name] = probe["value"]
        binding, _, kind = _runtime_binding_identity(name)
        coverage[binding] = {"availability": "AVAILABLE" if ok else "UNKNOWN", "output_name": name,
            "value_kind": kind, "conclusion_complete": ok,
            "reason_code": "" if ok else outcomes.get(name, {}).get("reason_code") or "RUNTIME_PROBE_OUTPUT_INCOMPLETE"}
    return {"resolved_runtime_paths": paths, "coverage": coverage}


def _run_probe_driver(argv, *, timeout, check=False, capture_output=True, text=True, **kwargs):
    # Give the driver's finally blocks a chance to retire launcher groups on an
    # outer deadline too; subprocess.run(timeout) kills the driver immediately.
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=text, start_new_session=True, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
        raise


def inspect_declared_runtime(requirements: Mapping, prefix: Path, *, runner=None) -> dict:
    spec = build_probe_specification(requirements, prefix)
    result = (runner or _run_probe_driver)(probe_argv(spec), env=spec["environment"], cwd="/tmp",
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0 or len(result.stdout.encode()) > 65536:
        raise ValueError("RUNTIME_PROBE_EXECUTION_INCOMPLETE")
    return parse_probe_result(result.stdout, spec)
