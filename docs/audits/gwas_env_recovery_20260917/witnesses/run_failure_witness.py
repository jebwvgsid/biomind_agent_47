"""Rebuild and run the failure audit from durable sources; no old /tmp needed.

Only the offline subprocess imports BioMind. This launcher uses the standard
library, an exact hash-checked bootstrap, and the unchanged witness body.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from failure_capture import capture


BOOTSTRAP_SHA256 = "f00f12f8f68c872cacdf265e7edfde5e79df6eaaa3573974e61f1bbe77e8ebc0"
WITNESS_SHA256 = "644a4e5918958a4eac2729f70a9b39c29ebc98b897e9651a53855ae796818ec9"
DEFAULT_PYTHON = "/public/home/liusiqi/anaconda3/envs/biomind/bin/python"
DEFAULT_SESSION = "/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789648505"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_file(path, expected):
    if path.is_symlink() or not path.is_file() or digest(path) != expected:
        raise RuntimeError("AUDIT_SOURCE_HASH_MISMATCH: " + str(path))


def stage(source, candidate, expected, bootstrap, witness):
    if not source.is_dir():
        raise RuntimeError("PERSISTENT_SOURCE_MISSING: " + str(source))
    candidate.mkdir()
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(source)
        # Never copy/read config, startup or a symlink into the safe candidate.
        if relative.parts[0] in {"config", "tests", "skills"} or path.name == "startup.py":
            continue
        if path.is_symlink():
            raise RuntimeError("SOURCE_SYMLINK_NOT_ALLOWED: " + str(path))
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    (candidate / "tests").mkdir()
    shutil.copyfile(bootstrap, candidate / "tests/offline_managed_bootstrap.py")
    shutil.copyfile(witness, candidate / "tests/failure_witness.py")
    for relative, identity in expected.items():
        check_file(candidate / relative, identity["sha256"])
    return {str(path.relative_to(candidate)): digest(path)
            for path in sorted(candidate.rglob("*.py"))}


def protected_changes(snapshot):
    original = json.loads((snapshot / "protected_hashes.json").read_text())
    return [name for name, metadata in original.items()
            if not Path(name).is_file() or digest(Path(name)) != metadata["sha256"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-only", action="store_true", help="Capture and validate source hashes without importing or testing BioMind.")
    parser.add_argument("--python", default=DEFAULT_PYTHON, help="Existing interpreter; this launcher installs nothing.")
    parser.add_argument("--session", type=Path, default=Path(DEFAULT_SESSION))
    parser.add_argument("--output", type=Path, help="New JSON evidence path; existing output is protected.")
    args = parser.parse_args()
    witness_root = Path(__file__).resolve().parent
    audit_root = witness_root.parent
    repository = audit_root.parents[2]
    bootstrap = repository / "tests/offline_managed_bootstrap.py"
    witness = witness_root / "failure_witness.py"
    check_file(bootstrap, BOOTSTRAP_SHA256)
    check_file(witness, WITNESS_SHA256)
    prior = json.loads((audit_root / "evidence/failure_witness_results.json").read_text())
    run_root = Path(tempfile.mkdtemp(prefix="biomind-gwas-failure-reproduction-", dir="/tmp"))
    snapshot = run_root / "session_snapshot"
    capture(args.session.resolve(), snapshot, run_root / "recorded.json")
    observed = json.loads((run_root / "recorded.json").read_text())
    expected_recording = json.loads((audit_root / "evidence/failure_recorded.json").read_text())
    expected_sources = {Path(row["path"]).name: row["sha256"] for row in expected_recording["source_files"]}
    for row in observed["source_files"]:
        if expected_sources.get(Path(row["path"]).name) != row["sha256"]:
            raise RuntimeError("INCIDENT_SOURCE_CHANGED: " + row["path"])
    summary = {"mode": "stage_only" if args.stage_only else "replayed_current_behavior",
        "run_root": str(run_root), "snapshot": str(snapshot),
        "bootstrap_sha256": BOOTSTRAP_SHA256, "witness_sha256": WITNESS_SHA256,
        "capture_script_sha256": digest(witness_root / "failure_capture.py"),
        "incident_sources_match_recorded": True, "versions": {},
        "boundaries": ["No old /tmp candidate or snapshot is required.",
            "No production write, real configuration/credential import, API, model, installation or science execution.",
            "The recorded executor return is the only execution substitute; real reconciliation, TypedExecute, causal-packet builder and current-stop/CLI functions run unchanged.",
            "Offline bootstrap provides inert configuration and blocks original-workspace reads, network and unapproved process execution.",
            "Stage-only validates copied source identities but is not a test pass."]}
    failed = False
    for label, selection in (("current", []), ("prerequisite", ["-k", "reconciler or causal"])):
        previous = prior["versions"][label]
        source = Path(previous["source_root"])
        candidate = run_root / (label + "_candidate")
        manifest = stage(source, candidate, previous["imported_source_hashes"], bootstrap, witness)
        temp = run_root / (label + "_tmp")
        temp.mkdir()
        results = run_root / (label + "_results.json")
        artifacts = run_root / (label + "_artifacts")
        log_path = run_root / (label + ".log")
        environment = {"PATH": "/usr/bin:/bin", "TMPDIR": str(temp), "BIOMIND_DISABLE_DOTENV": "1",
            "FAILURE_SNAPSHOT": str(snapshot), "FAILURE_RESULTS": str(artifacts),
            "BIOMIND_OFFLINE_RESULTS": str(results)}
        command = [args.python, str(candidate / "tests/offline_managed_bootstrap.py"), str(candidate),
            "-q", "tests/failure_witness.py", *selection]
        item = {"persistent_source": str(source), "candidate": str(candidate),
            "matched_prior_imported_sources": len(previous["imported_source_hashes"]),
            "source_manifest": manifest, "command_argv": command, "clean_environment": environment,
            "log_path": str(log_path)}
        if not args.stage_only:
            with log_path.open("wb") as output:
                process = subprocess.run(command, cwd=run_root, env=environment, stdout=output,
                    stderr=subprocess.STDOUT, timeout=120, check=False)
            text = log_path.read_text()
            reports = json.loads(results.read_text()) if results.is_file() else []
            checks = [row for row in reports if row.get("phase") == "call"]
            count = 3 if label == "current" else 2
            item.update(returncode=process.returncode, reports=reports,
                source_unchanged="SOURCE_UNCHANGED True" in text,
                passed=len(checks) == count and all(row.get("outcome") == "passed" for row in checks),
                artifacts={path.name: json.loads(path.read_text()) for path in sorted(artifacts.glob("*.json"))})
            failed = failed or process.returncode != 0 or not item["source_unchanged"] or not item["passed"]
        summary["versions"][label] = item
    changed = protected_changes(snapshot)
    summary.update(protected_file_count=observed["protected_file_count"],
        protected_files_unchanged=not changed, changed_paths=changed)
    failed = failed or bool(changed)
    summary["successful"] = not failed
    destination = args.output.resolve() if args.output else run_root / "reproduction.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"evidence": str(destination), "run_root": str(run_root),
        "mode": summary["mode"], "successful": not failed}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
