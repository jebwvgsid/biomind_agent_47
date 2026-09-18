"""Prepare one approved immutable BWA/Picard bundle; never accept or Commit it.

The local publication journal is an engineering receipt, not Plan authority.
This helper must run through the approved Execute path with exact bound tools.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from agent_core.context_lineage import _atomic_replace_json, _fsync_directory
from agent_core.genomics_reference_bundle import (
    ReferenceBundleError, reference_file_uri, validate_reference_bundle_files,
    validate_reference_component,
)
from agent_core.step_effects import STRUCTURED_EFFECT_EVIDENCE_PREFIX

_COMPONENTS = {
    "fai": ("reference.fa.fai",),
    "bwa": tuple("reference.fa." + suffix for suffix in ("amb", "ann", "bwt", "pac", "sa")),
    "dictionary": ("reference.dict",),
}


class PreparationError(ValueError):
    def __init__(self, code, detail="", *, returncode=2):
        self.code, self.detail, self.returncode = code, detail, returncode
        super().__init__(code + (": " + str(detail) if detail else ""))


def _require(condition, code, detail=""):
    if not condition:
        raise PreparationError(code, detail)


def _safe_path(value):
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts, "REFERENCE_PREPARATION_PATH_INVALID", value)
    for component in (path, *path.parents):
        _require(not component.is_symlink(), "REFERENCE_PREPARATION_SYMLINK_REJECTED", component)
    return path


def _sha(path):
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inode(path):
    value = path.stat(follow_symlinks=False)
    _require(stat.S_ISDIR(value.st_mode), "REFERENCE_PREPARATION_DIRECTORY_REQUIRED", path)
    return {"device": value.st_dev, "inode": value.st_ino}


def _write_journal(path, journal):
    # Existing durable JSON writer; local receipt has no mutation/Commit authority.
    _atomic_replace_json(path, journal)


def _publish_noreplace(source, target):
    """No overwrite window, including publishers which do not share our lock."""
    _require(source.stat().st_dev == target.parent.stat().st_dev,
             "REFERENCE_PUBLICATION_CROSS_DEVICE")
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    _require(rename is not None, "REFERENCE_PUBLICATION_NOREPLACE_UNSUPPORTED")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise PreparationError("REFERENCE_OUTPUT_CONFLICT", str(target))
        if error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise PreparationError("REFERENCE_PUBLICATION_NOREPLACE_UNSUPPORTED", os.strerror(error))
        raise PreparationError("REFERENCE_PUBLICATION_FAILED", os.strerror(error))
    _fsync_directory(source.parent)
    if source.parent != target.parent:
        _fsync_directory(target.parent)


def _command(argv, *, cwd, env, stdout_path, stderr_path):
    with stdout_path.open("xb") as out, stderr_path.open("xb") as err:
        result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err, check=False)
        out.flush(); err.flush()
        os.fsync(out.fileno()); os.fsync(err.fileno())
    return result.returncode


def _identity_environment(env):
    values = {name: str(env.get("BIOMIND_" + name.upper() + "_ID") or "")
              for name in ("task", "step", "action", "attempt")}
    _require(all(values.values()), "REFERENCE_EXECUTION_IDENTITY_REQUIRED")
    return values


def prepare_reference(*, source, output, work_dir, tools, reference_ref, artifact_ref, evidence_artifact_ref,
                      environment=None):
    env = dict(os.environ if environment is None else environment)
    identity = _identity_environment(env)
    source, output, work = (_safe_path(value) for value in (source, output, work_dir))
    _require(source.is_file() and output.parent.is_dir() and work.parent.is_dir(),
             "REFERENCE_PREPARATION_PARENT_OR_INPUT_MISSING")
    _require(not source.is_relative_to(output) and not source.is_relative_to(work)
             and not output.is_relative_to(work) and not work.is_relative_to(output),
             "REFERENCE_PREPARATION_PATH_OVERLAP")
    _require(output.parent.stat().st_dev == work.parent.stat().st_dev,
             "REFERENCE_PUBLICATION_CROSS_DEVICE")
    _require(reference_ref and artifact_ref and evidence_artifact_ref, "REFERENCE_FORMAL_RESOURCE_REF_REQUIRED")
    resolved_tools = {name: Path(tools[name]) for name in ("bwa", "samtools", "picard", "java")}
    _require(all(path.is_absolute() and ".." not in path.parts for path in resolved_tools.values()),
             "REFERENCE_BOUND_TOOL_PATH_INVALID")
    for name, tool in resolved_tools.items():
        _require(tool.is_file() and os.access(tool, os.X_OK), "REFERENCE_BOUND_TOOL_UNAVAILABLE", name)
    scope = {"task_id": identity["task"], "step_id": identity["step"],
             "source": str(source), "source_sha256": _sha(source), "output": str(output),
             "work_dir": str(work), "reference_resource_ref": reference_ref,
             "artifact_ref": artifact_ref, "evidence_artifact_ref": evidence_artifact_ref,
             "tools": {name: {"path": str(path), "resolved_target": str(path.resolve(strict=True)),
                              "sha256": _sha(path)} for name, path in resolved_tools.items()}}
    created = False
    try:
        work.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        _require(work.is_dir(), "REFERENCE_WORK_DIRECTORY_CONFLICT")
    owner = work / "owner.json"
    if created:
        with owner.open("x", encoding="utf-8") as stream:
            json.dump(scope, stream, sort_keys=True, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        _fsync_directory(work)
    _require(owner.is_file() and not owner.is_symlink(), "REFERENCE_WORK_DIRECTORY_UNOWNED")
    _require(json.loads(owner.read_text(encoding="utf-8")) == scope,
             "REFERENCE_WORK_DIRECTORY_OWNER_MISMATCH")
    lock_fd = os.open(work / ".publication.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PreparationError("REFERENCE_PREPARATION_ALREADY_RUNNING") from exc
        journal_path = work / "publication.json"
        _require(not journal_path.is_symlink(), "REFERENCE_JOURNAL_SYMLINK_REJECTED")
        journal = json.loads(journal_path.read_text()) if journal_path.exists() else {
            "schema_version": 1, "kind": "skill_reference_publication", "scope": scope, "attempts": []}
        _require(journal.get("scope") == scope and journal.get("schema_version") == 1,
                 "REFERENCE_PUBLICATION_SCOPE_MISMATCH")
        expected_uri = reference_file_uri(output / "reference.fa")

        def validate(directory, component=None):
            args = {"expected_final_reference": output / "reference.fa", "require_final_uri": True}
            return (validate_reference_component(source, directory, component, **args) if component
                    else validate_reference_bundle_files(source, directory, **args))

        if os.path.lexists(output):
            prepared = journal.get("prepared") or {}
            _require(not output.is_symlink() and prepared.get("directory_identity") == _inode(output),
                     "REFERENCE_OUTPUT_CONFLICT", output)
            checked = validate(output)
            _require(checked["files"] == prepared.get("validation", {}).get("files"),
                     "REFERENCE_PUBLISHED_CONTENT_CHANGED")
            journal.update(status="published", recovered_after_publish=True,
                           publication_validation=checked, last_observed_identity=identity)
            _write_journal(journal_path, journal)
            return {"status": "published_uncommitted", "reused_published": True,
                    "validation": checked, "journal_path": str(journal_path), "identity": identity,
                    "commands": [], "output": str(output), "expected_final_uri": expected_uri}

        old_attempts = list(journal["attempts"])
        stage = work / ("build-" + uuid.uuid4().hex)
        stage.mkdir(mode=0o700)
        build = {"identity": identity, "staging": str(stage), "directory_identity": _inode(stage),
                 "status": "building", "commands": [], "components": {}}
        journal["attempts"].append(build)
        journal.update(status="building")
        _write_journal(journal_path, journal)
        try:
            with source.open("rb") as inp, (stage / "reference.fa").open("xb") as out:
                shutil.copyfileobj(inp, out, 1024 * 1024)
                out.flush(); os.fsync(out.fileno())
            validate(stage, "fasta")
            command_env = dict(env)
            java_prefix = resolved_tools["java"].parent.parent
            command_env.update(JAVA_HOME=str(java_prefix), PATH=str(java_prefix / "bin") + ":/usr/bin:/bin")
            commands = {
                "bwa": [str(resolved_tools["bwa"]), "index", str(stage / "reference.fa")],
                "fai": [str(resolved_tools["samtools"]), "faidx", str(stage / "reference.fa")],
                "dictionary": [str(resolved_tools["picard"]), "CreateSequenceDictionary",
                               "R=" + str(stage / "reference.fa"), "O=" + str(stage / "reference.dict"),
                               "URI=" + expected_uri],
            }
            for component in ("bwa", "fai", "dictionary"):
                reused = False
                for old in reversed(old_attempts):
                    receipt = old.get("components", {}).get(component)
                    if not receipt:
                        continue
                    old_stage = _safe_path(old["staging"])
                    _require(old_stage.parent == work and old_stage.name.startswith("build-"),
                             "REFERENCE_RESIDUE_SCOPE_MISMATCH")
                    if not old_stage.exists() or _inode(old_stage) != old.get("directory_identity"):
                        continue
                    try:
                        checked = validate(old_stage, component)
                    except ReferenceBundleError:
                        continue
                    if checked.get("files") != receipt.get("validation", {}).get("files"):
                        continue
                    for name in _COMPONENTS[component]:
                        with (old_stage / name).open("rb") as inp, (stage / name).open("xb") as out:
                            shutil.copyfileobj(inp, out, 1024 * 1024)
                            out.flush(); os.fsync(out.fileno())
                    copied = validate(stage, component)
                    _require(copied["files"] == checked["files"], "REFERENCE_REUSED_COMPONENT_CHANGED")
                    checked = copied
                    build["components"][component] = {"validation": checked, "reused_from": str(old_stage),
                                                       "source_attempt": old.get("identity")}
                    reused = True
                    _write_journal(journal_path, journal)
                    break
                if reused:
                    continue
                number = len(build["commands"]) + 1
                prefix = work / f"{stage.name}-command-{number:02d}-{component}"
                row = {"component": component, "argv": commands[component], "status": "started",
                       "stdout_path": str(prefix) + ".stdout", "stderr_path": str(prefix) + ".stderr"}
                build["commands"].append(row)
                _write_journal(journal_path, journal)
                tool_name = {"bwa": "bwa", "fai": "samtools", "dictionary": "picard"}[component]
                for tool_key in (tool_name, *(["java"] if component == "dictionary" else [])):
                    tool = resolved_tools[tool_key]
                    _require(str(tool.resolve(strict=True)) == scope["tools"][tool_key]["resolved_target"]
                             and _sha(tool) == scope["tools"][tool_key]["sha256"],
                             "REFERENCE_BOUND_TOOL_CHANGED", tool_key)
                rc = _command(commands[component], cwd=stage, env=command_env,
                              stdout_path=Path(row["stdout_path"]), stderr_path=Path(row["stderr_path"]))
                row.update(returncode=rc, status="returned")
                _write_journal(journal_path, journal)
                if rc != 0:
                    raise PreparationError("REFERENCE_REQUIRED_COMMAND_FAILED", component, returncode=rc)
                checked = validate(stage, component)
                build["components"][component] = {"validation": checked, "command_index": number - 1}
                _write_journal(journal_path, journal)
            checked = validate(stage)
            _require(_sha(source) == scope["source_sha256"], "REFERENCE_SOURCE_CHANGED")
            # Tools close outputs, but durable publication also requires fsync
            # of every independently checked file before the directory receipt.
            for name in checked["files"]:
                with (stage / name).open("rb") as stream:
                    os.fsync(stream.fileno())
            _fsync_directory(stage)
            journal["prepared"] = {"directory_identity": _inode(stage), "staging": str(stage),
                                   "validation": checked, "identity": identity}
            build["status"] = "prepared"
            journal["status"] = "prepared"
            _write_journal(journal_path, journal)
            _publish_noreplace(stage, output)
            checked = validate(output)
            _require(checked["files"] == journal["prepared"]["validation"]["files"],
                     "REFERENCE_PUBLISHED_CONTENT_CHANGED")
            build["status"] = "published"
            journal.update(status="published", publication_validation=checked)
            _write_journal(journal_path, journal)
            return {"status": "published_uncommitted", "reused_published": False,
                    "validation": checked, "journal_path": str(journal_path), "identity": identity,
                    "commands": build["commands"], "output": str(output), "expected_final_uri": expected_uri}
        except (PreparationError, ReferenceBundleError, OSError) as exc:
            build.update(status="failed", failure={"code": getattr(exc, "code", type(exc).__name__),
                         "detail": str(exc), "returncode": getattr(exc, "returncode", None)})
            journal["status"] = "failed"
            _write_journal(journal_path, journal)
            raise
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "output", "work-dir", "reference-ref", "artifact-ref", "evidence-artifact-ref", "bwa", "samtools", "picard", "java"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare_reference(source=args.source, output=args.output, work_dir=args.work_dir,
            tools={name: getattr(args, name) for name in ("bwa", "samtools", "picard", "java")},
            reference_ref=args.reference_ref, artifact_ref=args.artifact_ref,
            evidence_artifact_ref=args.evidence_artifact_ref)
    except (PreparationError, ReferenceBundleError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "code": getattr(exc, "code", type(exc).__name__),
              "detail": str(exc), "child_returncode": getattr(exc, "returncode", None)}, ensure_ascii=False), file=sys.stderr)
        # Journal retains the original Python negative signal rc; the wrapper's
        # portable failure code never claims a native signal or inferred OOM.
        rc = getattr(exc, "returncode", 2)
        return rc if 0 < rc < 256 else 1
    facts = {"schema_version": 1,
        "produces_artifacts": [{"identity": args.artifact_ref, "path": args.output},
                               {"identity": args.evidence_artifact_ref, "path": args.work_dir}],
        "produces_observations": [{"identity": "reference_bundle_valid", "value": True,
                                   "helper_receipt": result}],
        "explicit_side_effects": [{"identity": "create_reference_indexes",
             "reference_resource_ref": args.reference_ref, "index_artifacts": [args.artifact_ref],
             "journal_path": result["journal_path"]}]}
    print(STRUCTURED_EFFECT_EVIDENCE_PREFIX + json.dumps(facts, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
