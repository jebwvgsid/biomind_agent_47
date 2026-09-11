"""Export explicitly permitted source only; never copy config, sessions or data.

No BioMind import occurs here. ``--tree`` reads the same allowlist from Git
blobs, so a proposed commit can be checked separately from a dirty worktree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess

SOURCE_DIRS = ("agent_core", "execution", "memory", "prompts")
ROOT_FILES = ("bio_agents.py", "memory_manager.py", "package_manager.py", "supervisor.py")
TEST_FILES = (
    "tests/__init__.py", "tests/offline_managed_bootstrap.py",
    "tests/ger_fixture_support.py", "tests/test_plan_local_preaction_convergence_v2.py",
    "tests/test_ger_combined_workflow.py", "tests/test_ger_observation_boundary.py",
    "tests/test_inspection_workflow.py", "tests/test_recovery_agent_loop.py",
    "tests/test_recovery_projection.py", "tests/test_recovery_reception.py",
    "tests/cli_model_progress_support.py", "tests/export_cli_model_progress_fixture.py",
    "tests/test_cli_model_progress_integration.py", "tests/test_cli_model_recovery_tools.py",
    "tests/test_cli_model_display.py", "tests/test_cli_model_progress.py",
    "tests/test_cli_cp_public_integration.py", "tests/test_cli_public_response.py",
)
RESOURCE_FILES = ("skills/sequence/search/local-blast/SKILL.md",)
GIT_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1"}


def allowed(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or any(part.startswith(".") for part in path.parts):
        return False
    return (name in ROOT_FILES or name in TEST_FILES or name in RESOURCE_FILES
            or (path.parts[0] in SOURCE_DIRS and path.suffix == ".py"))


def git(repository: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repository), *args], env=GIT_ENV)


def export(repository: Path, destination: Path, tree: str | None = None) -> dict:
    repository = repository.resolve()
    destination = destination.resolve()
    if not destination.is_relative_to(Path("/tmp")) or destination == Path("/tmp"):
        raise ValueError("The isolated export must be a new directory below /tmp")
    destination.mkdir(parents=True, exist_ok=False)
    if tree:
        resolved = git(repository, "rev-parse", "--verify", f"{tree}^{{tree}}").decode().strip()
        names = git(repository, "ls-tree", "-r", "--name-only", resolved, "--",
                    *SOURCE_DIRS, *ROOT_FILES, "tests", *RESOURCE_FILES).decode().splitlines()
        read = lambda name: git(repository, "show", f"{resolved}:{name}")
    else:
        resolved = "exact_worktree"
        names = [str(path.relative_to(repository)) for directory in SOURCE_DIRS
                 for path in (repository / directory).rglob("*.py")]
        names += [name for name in (*ROOT_FILES, *TEST_FILES, *RESOURCE_FILES)
                  if (repository / name).is_file()]

        def read(name):
            path = repository / name
            if path.is_symlink() or not path.resolve().is_relative_to(repository):
                raise ValueError("Symlink source is not an approved export: " + name)
            return path.read_bytes()

    files = {}
    for name in sorted(set(names)):
        if not allowed(name):
            continue
        content = read(name)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        files[name] = hashlib.sha256(content).hexdigest()
    manifest = {"source": resolved, "files": files,
                "source_signature": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
                "excluded": ["config", "startup", "credentials", "sessions", "databases", "task_outputs", "old evidence fixtures"]}
    (destination / "export_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tree", help="Git commit/tree to export instead of the exact worktree")
    args = parser.parse_args()
    manifest = export(args.repository, args.destination, args.tree)
    print(json.dumps({"destination": str(args.destination), "source": manifest["source"],
                      "files": len(manifest["files"]), "signature": manifest["source_signature"]}))


if __name__ == "__main__":
    main()
