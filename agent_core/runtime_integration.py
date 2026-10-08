"""Offline Node/npm diagnosis and optional, account-scoped Orca links.

No model/config/project imports, package installation, shell startup changes or
client connectivity claims. Version subprocesses use disposable configuration.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

from agent_core.pi_entry import MINIMUM_NODE, node_candidate, node_executable, node_version, PiEntryError

ORCA_MINIMUM_NODE = (18, 0, 0)  # Integration requirement, not the Pi requirement.
PROBE_TIMEOUT = 10  # Same bound as the existing pi_entry version check.
SCHEMA = "biomind-orca-links-v1"


def failure(error: OSError, phase: str, path: Path) -> dict:
    return {"phase": phase, "path": str(path), "errno": error.errno,
            "error": errno.errorcode.get(error.errno, "OS_ERROR")}


def identity(path: Path) -> dict:
    info = path.lstat()
    return {"dev": info.st_dev, "ino": info.st_ino, "ctime_ns": info.st_ctime_ns}


def owned(item: dict) -> bool:
    path = Path(item["destination"])
    try:
        return (path.is_symlink() and os.readlink(path) == item["source"]
                and identity(path) == item["identity"])
    except OSError:
        return False


def probe(argv: list[str], path: str, selected_node: Path | None = None) -> dict:
    """Only version calls. Never inherit credentials, NODE_OPTIONS or npm config.

    /tmp is explicit: an inherited TMPDIR must not redirect probes into HOME.
    stdout/stderr in the report are bounded; caller's environment is never dumped.
    """
    result = {"argv": argv, "environment": "SANITIZED", "temporary_io": "cleaned /tmp probe directory"}
    with tempfile.TemporaryDirectory(prefix="biomind-version-", dir="/tmp") as directory:
        root = Path(directory)
        if selected_node is not None:
            (root / "bin").mkdir()
            (root / "bin/node").symlink_to(selected_node)
            path = str(root / "bin") + os.pathsep + path
            result["node_binding"] = str(selected_node)
        for name in ("user-npmrc", "global-npmrc"):
            (root / name).write_text("", encoding="utf-8")
        env = {"PATH": path, "HOME": directory, "LC_ALL": "C", "TMPDIR": directory,
               "XDG_CONFIG_HOME": directory, "XDG_CACHE_HOME": directory,
               "NPM_CONFIG_USERCONFIG": str(root / "user-npmrc"),
               "NPM_CONFIG_GLOBALCONFIG": str(root / "global-npmrc"),
               "NPM_CONFIG_CACHE": str(root / "cache"), "NPM_CONFIG_PREFIX": str(root / "prefix"),
               "NPM_CONFIG_UPDATE_NOTIFIER": "false"}
        try:
            completed = subprocess.run(argv, env=env, cwd=directory, capture_output=True,
                                       text=True, errors="replace", timeout=PROBE_TIMEOUT)
            result.update(rc=completed.returncode, stdout=completed.stdout[:2048].strip(),
                          stderr=completed.stderr[:2048].strip())
            version = node_version(result["stdout"])
            result.update(status="OK" if completed.returncode == 0 and version else "FAILED",
                          version=list(version) if version else None)
        except subprocess.TimeoutExpired:
            result.update(status="TIMEOUT", rc=None, timeout_seconds=PROBE_TIMEOUT)
        except OSError as error:
            result.update(status="FAILED", rc=None, **failure(error, "version", Path(argv[0])))
    return result


def compatible(result: dict, minimum: tuple[int, int, int]) -> bool:
    return result.get("status") == "OK" and tuple(result.get("version") or ()) >= minimum


def directory_info(path: Path) -> dict:
    ancestor = path
    missing = []
    while not os.path.lexists(ancestor):
        missing.append(str(ancestor))
        ancestor = ancestor.parent
    result = {"path": str(path), "missing_directories": list(reversed(missing)),
              "writability": "NOT_VERIFIED", "checked_ancestor": str(ancestor)}
    try:
        result["readonly_hint"] = bool(os.statvfs(ancestor).f_flag & os.ST_RDONLY)
        result["access_write_hint"] = os.access(ancestor, os.W_OK)
        result["ancestor_is_directory"] = ancestor.is_dir()
    except OSError as error:
        result["metadata_error"] = failure(error, "directory_metadata", ancestor)
    return result


def read_receipt(path: Path, bin_dir: Path) -> dict | None:
    if not os.path.lexists(path):
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 65536:
            raise ValueError("Receipt must be a bounded regular file owned by this account")
        data = json.load(stream)
    if (not isinstance(data, dict) or data.get("schema") != SCHEMA
            or data.get("bin_dir") != str(bin_dir) or not isinstance(data.get("created"), list)):
        raise ValueError("Receipt schema or destination does not match this operation")
    seen = set()
    for item in data["created"]:
        if (not isinstance(item, dict) or item.get("destination") not in {str(bin_dir / n) for n in ("node", "npm")}
                or item["destination"] in seen or not isinstance(item.get("source"), str)
                or not Path(item["source"]).is_absolute() or not isinstance(item.get("identity"), dict)
                or set(item["identity"]) != {"dev", "ino", "ctime_ns"}):
            raise ValueError("Receipt link identity is invalid")
        seen.add(item["destination"])
    return data


def diagnose(runtime: Path, home: Path, bin_dir: Path, environment: dict, explicit: Path | None = None) -> dict:
    runtime = runtime.resolve()
    path = environment.get("PATH", os.defpath)
    receipt_path = home / ".local/state/biomind/orca-links.json"
    candidate, source = node_candidate(explicit, environment, runtime)
    internal = {"source": source, "entry": str(candidate), "minimum": list(MINIMUM_NODE)}
    try:
        node = node_executable(explicit, environment, runtime)
        internal["resolved"] = str(node)
        internal["probe"] = probe([str(node), "--version"], path)
    except (PiEntryError, OSError, RuntimeError) as error:
        internal["probe"] = {"status": "MISSING_OR_UNEXECUTABLE", "error_type": type(error).__name__}
        node = candidate
    internal["compatible"] = compatible(internal["probe"], MINIMUM_NODE)
    # Bounded candidates only; no server-wide search and no same-directory rule.
    npm_candidates = [candidate.parent / "npm", node.parent / "npm"]
    found_npm = shutil.which("npm", path=path)
    if found_npm:
        npm_candidates.append(Path(found_npm))
    npm = next((p for p in npm_candidates if p.is_file() and os.access(p, os.X_OK)), npm_candidates[0])
    npm_report = {"entry": str(npm), "resolved": str(npm.resolve()),
                  "via_node": probe([str(node), str(npm.resolve()), "--version"], path),
                  "current_path": probe([str(npm), "--version"], path),
                  "paired_path": probe([str(npm), "--version"], path, node)}
    discovered = {name: shutil.which(name, path=path) for name in ("node", "npm")}
    external = {"entries": discovered, "minimum_node": list(ORCA_MINIMUM_NODE),
                "scope": "current process PATH; sanitized version subprocesses"}
    for name, entry in discovered.items():
        external[name] = probe([entry, "--version"], path) if entry else {"status": "MISSING"}
    external["compatible"] = compatible(external["node"], ORCA_MINIMUM_NODE) and external["npm"]["status"] == "OK"
    links = []
    for name, src in (("node", candidate), ("npm", npm)):
        dest = bin_dir / name
        try:
            same = dest.is_symlink() and dest.resolve() == src.resolve()
        except (OSError, RuntimeError):
            same = False
        links.append({"name": name, "source": str(src), "destination": str(dest),
                      "action": "EXISTING_UNOWNED" if same else "CONFLICT" if os.path.lexists(dest) else "CREATE"})
    result = {"schema": "biomind-runtime-diagnostic-v1", "install_root": str(runtime.parent.parent),
              "internal_node": internal, "npm": npm_report, "current_path": external,
              "bin_directory": directory_info(bin_dir), "receipt_directory": directory_info(receipt_path.parent),
              "bin_in_path": str(bin_dir) in [os.path.abspath(p or os.curdir) for p in path.split(os.pathsep)],
              "receipt_path": str(receipt_path), "links": links, "receipt": None,
              "normal_ssh": "NOT_RUN", "orca_version": "UNKNOWN", "orca_probe": "NOT_RUN",
              "relay": "NOT_RUN", "biomind_end_to_end": "NOT_RUN",
              "impact": "Adding node/npm to a PATH directory changes command selection for other processes of this account."}
    try:
        receipt = read_receipt(receipt_path, bin_dir)
        result["receipt"] = receipt
        if receipt:
            for item in receipt["created"]:
                if owned(item):
                    for link in links:
                        if link["destination"] == item["destination"] and link["action"] == "EXISTING_UNOWNED":
                            link["action"] = "EXISTING_OWNED"
    except (OSError, ValueError) as error:
        result["receipt_error"] = (failure(error, "read_receipt", receipt_path) if isinstance(error, OSError)
                                   else {"error": "INVALID_RECEIPT"})
    if result.get("receipt_error") or (result["receipt"] and result["receipt"].get("phase") not in {"APPLIED", "UNDONE", "ROLLED_BACK"}):
        result["status"] = "RECEIPT_NEEDS_ATTENTION"
    elif external["compatible"]:
        result["status"] = "NO_CHANGES_NEEDED"
    elif any(link["action"] == "CONFLICT" for link in links):
        result["status"] = "CONFLICT"
    elif any(entry and os.path.abspath(entry) != str(bin_dir / name) for name, entry in discovered.items()):
        result["status"] = "SHADOW_RISK"
    elif not compatible(internal["probe"], ORCA_MINIMUM_NODE) or npm_report["paired_path"]["status"] != "OK":
        result["status"] = "RUNTIME_UNAVAILABLE"
    elif all(link["action"] in {"EXISTING_OWNED", "EXISTING_UNOWNED"} for link in links):
        result["status"] = "NO_CHANGES_NEEDED"
    elif result["bin_directory"].get("readonly_hint") or result["receipt_directory"].get("readonly_hint"):
        result["status"] = "READONLY_HINT"
    else:
        result["status"] = "READY_TO_APPLY"
    return result


def write_receipt(path: Path, data: dict) -> None:
    """Atomic checkpoint; keep the previous pending receipt on write failure.

    Operations hold an advisory flock on the state directory. Initial creation
    uses exclusive semantics; symlink/user-file receipts are never followed.
    """
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    if not os.path.lexists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return
    previous = identity(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise OSError(errno.EEXIST, "Receipt replaced", str(path))
    fd, filename = tempfile.mkstemp(prefix=".orca-receipt-", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if identity(path) != previous:
            raise OSError(errno.EEXIST, "Receipt changed", str(path))
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


@contextmanager
def state_lock(parent: Path):
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def undo_plan(report: dict) -> dict:
    if report.get("receipt_error"):
        return {"status": "RECEIPT_NEEDS_ATTENTION", "links": []}
    receipt = report["receipt"]
    if not receipt:
        return {"status": "NO_OWNED_LINKS", "links": []}
    links = []
    for item in receipt["created"]:
        state = "REMOVE" if owned(item) else "REPLACED" if os.path.lexists(item["destination"]) else "ABSENT"
        links.append({**item, "action": state})
    return {"status": "UNDO_PREVIEW", "links": links, "receipt_path": report["receipt_path"],
            "pending_destination": receipt.get("pending_destination")}


def apply_links(report: dict, refresh) -> dict:
    """Only called after interactive approval of this report's exact scope."""
    if report["status"] == "NO_CHANGES_NEEDED":
        return {"status": "NO_CHANGES_NEEDED"}
    if report["status"] not in {"READY_TO_APPLY", "READONLY_HINT"}:
        return {"status": "REFUSED", "reason": report["status"]}
    path = Path(report["receipt_path"])
    stage, failed_path = "receipt_directory", path.parent
    created: list[dict] = []
    data = {"schema": SCHEMA, "install_root": report["install_root"],
            "bin_dir": report["bin_directory"]["path"], "phase": "APPLYING", "created": created,
            "time": datetime.now(timezone.utc).isoformat()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with state_lock(path.parent):
            fresh = refresh()
            # Sources/destinations and mkdir scope must still equal what was shown.
            for key in ("links", "install_root", "receipt_path", "bin_in_path"):
                if fresh[key] != report[key]:
                    return {"status": "REFUSED", "reason": "PLAN_CHANGED_REVIEW_AGAIN"}
            if (fresh["internal_node"]["probe"].get("version") != report["internal_node"]["probe"].get("version")
                    or fresh["npm"]["paired_path"].get("version") != report["npm"]["paired_path"].get("version")
                    or fresh["npm"]["resolved"] != report["npm"]["resolved"]):
                return {"status": "REFUSED", "reason": "RUNTIME_CHANGED_REVIEW_AGAIN"}
            if not set(fresh["bin_directory"]["missing_directories"]).issubset(report["bin_directory"]["missing_directories"]):
                return {"status": "REFUSED", "reason": "DIRECTORY_SCOPE_CHANGED"}
            if fresh["status"] not in {"READY_TO_APPLY", "READONLY_HINT"}:
                return {"status": "REFUSED", "reason": fresh["status"]}
            if fresh["receipt"]:
                # Retain ownership of any previous, still-identical links.
                created.extend(item for item in fresh["receipt"]["created"] if owned(item))
            prior = list(created)
            stage, failed_path = "receipt", path
            write_receipt(path, data)  # Durable pending intent before link writes.
            try:
                stage, failed_path = "bin_directory", Path(data["bin_dir"])
                failed_path.mkdir(parents=True, exist_ok=True)
                for link in fresh["links"]:
                    if link["action"] != "CREATE":
                        continue
                    dest = Path(link["destination"])
                    data["pending_destination"] = str(dest)
                    stage, failed_path = "receipt", path
                    write_receipt(path, data)
                    stage, failed_path = "symlink", dest
                    os.symlink(link["source"], dest)  # Never overwrites, including races/dangling links.
                    created.append({"destination": str(dest), "source": link["source"], "identity": identity(dest)})
                    data.pop("pending_destination", None)
                    stage, failed_path = "receipt", path
                    write_receipt(path, data)
                stage, failed_path = "verify", Path(data["bin_dir"])
                verify_path = str(failed_path) + os.pathsep + os.defpath
                verified = {name: probe([str(failed_path / name), "--version"], verify_path) for name in ("node", "npm")}
                if not compatible(verified["node"], ORCA_MINIMUM_NODE) or verified["npm"]["status"] != "OK":
                    raise OSError(errno.EIO, "Post-create version verification failed", str(failed_path))
                if any(not owned(item) for item in created):
                    raise OSError(errno.EEXIST, "Link identity changed", str(failed_path))
                data.update(phase="APPLIED", verification=verified)
                stage, failed_path = "receipt", path
                write_receipt(path, data)
                return {"status": "APPLIED", "created": created, "verification": verified,
                        "receipt_path": str(path), "orca_probe": "NOT_RUN", "relay": "NOT_RUN"}
            except OSError as error:
                problem = failure(error, stage, failed_path)
                rollback_errors, residual = [], []
                for item in reversed(created[len(prior):]):
                    dest = Path(item["destination"])
                    try:
                        if owned(item):
                            dest.unlink()
                        elif os.path.lexists(dest):
                            raise OSError(errno.EEXIST, "Link replaced; not removed", str(dest))
                    except OSError as rollback_error:
                        rollback_errors.append(failure(rollback_error, "rollback", dest))
                        residual.append(str(dest))
                data["created"] = prior + [item for item in created[len(prior):] if os.path.lexists(item["destination"])]
                data.update(phase="PARTIAL" if residual else "ROLLED_BACK", error=problem)
                data.pop("pending_destination", None)
                receipt_error = None
                try:
                    write_receipt(path, data)
                except OSError as write_error:
                    receipt_error = failure(write_error, "receipt", path)
                return {"status": "PARTIAL" if residual else "FAILED", "error": problem,
                        "residual": residual, "rollback_errors": rollback_errors,
                        "receipt_error": receipt_error, "receipt_path": str(path),
                        "verification": locals().get("verified"),
                        "remaining_entries": data["created"],
                        "receipt_phase": "UNKNOWN_PENDING" if receipt_error else data["phase"]}
    except OSError as error:
        return {"status": "FAILED", "error": failure(error, stage, failed_path), "residual": [],
                "receipt_path": str(path), "receipt_phase": "CHECK_REQUIRED"}


def undo_links(report: dict, approved: dict) -> dict:
    path = Path(report["receipt_path"])
    removed = []
    try:
        with state_lock(path.parent):
            receipt = read_receipt(path, Path(report["bin_directory"]["path"]))
            fresh = undo_plan({**report, "receipt": receipt})
            if fresh != approved:
                return {"status": "REFUSED", "reason": "UNDO_PLAN_CHANGED"}
            errors = []
            for item in receipt["created"]:
                dest = Path(item["destination"])
                try:
                    if owned(item):
                        dest.unlink()
                        removed.append(str(dest))
                    elif os.path.lexists(dest):
                        raise OSError(errno.EEXIST, "Replaced link preserved", str(dest))
                except OSError as error:
                    errors.append(failure(error, "undo", dest))
            receipt["created"] = [item for item in receipt["created"] if os.path.lexists(item["destination"])]
            pending = receipt.get("pending_destination")
            if pending and os.path.lexists(pending) and pending not in {item["destination"] for item in receipt["created"]}:
                errors.append({"phase": "undo", "path": pending, "error": "UNRECORDED_IDENTITY_PRESERVED"})
            receipt["phase"] = "PARTIAL" if errors else "UNDONE"
            write_receipt(path, receipt)
            return {"status": "PARTIAL" if errors else "UNDONE", "removed": removed, "errors": errors}
    except (OSError, ValueError) as error:
        return {"status": "FAILED", "error": failure(error, "undo_receipt", path) if isinstance(error, OSError)
                else {"error": "INVALID_RECEIPT"}, "removed": removed,
                "receipt_phase": "CHECK_REQUIRED"}


def export_handoff(directory: Path, report: dict, explicit: Path | None, bin_dir: Path) -> dict:
    launcher = Path(report["install_root"]) / "bin/biomind"
    base = [str(launcher), "integrate", "orca", "--bin-dir", str(bin_dir)]
    if explicit is not None:
        base += ["--pi-node", str(explicit.expanduser().absolute())]
    command = shlex.join(base)
    doctor = shlex.join([str(launcher), "doctor", "--integration", "orca", "--bin-dir", str(bin_dir)]
                        + (["--pi-node", str(explicit.expanduser().absolute())] if explicit else []))
    note = ("# Normal SSH handoff / 正常 SSH 交接\n\n"
            f"Installation: `{report['install_root']}`\n\n"
            f"Current execution view: `{report['status']}`. Ordinary SSH / Windows Orca / relay: NOT_RUN.\n\n"
            "由用户在获准的正常 SSH 会话执行。当前工具没有部署真实链接。预览会影响账户 PATH 命令选择；"
            "不修改 shell 配置。rw/access 线索不保证可写；遇 EROFS 停止，不 sudo/remount/换通道。\n\n"
            f"```sh\n{doctor}\n{command}\n{command} --apply\n{command} --undo\n```\n\n"
            "Apply/undo require a terminal and the exact word yes; Enter/EOF refuses. "
            "Apply re-resolves and verifies runtime and displays paths again. "
            "If installation moved, regenerate this handoff; do not use stale paths. "
            "Undo only removes receipt-owned, unchanged links; replacement entries are preserved. "
            "State directories and the small receipt remain for diagnosis.\n\n"
            "Windows PowerShell (replace SERVER_ALIAS with your actual SSH target):\n\n"
            "```powershell\nssh -T SERVER_ALIAS 'hostname; id -un; command -v node; command -v npm; node --version; npm --version'\n```\n\n"
            "Record actual host/account and versions. New SSH discovery is separate from Orca. "
            "Record Windows Orca's installed version, reconnect, and record its actual Node/npm probe. "
            "No supported explicit runtime setting is assumed. Relay/node-pty deployment requires separate scope; "
            "this handoff does not deploy it or run a model/scientific analysis.\n\n"
            f"Receipt: `{report['receipt_path']}`\n\n"
            + "\n".join(f"- `{i['destination']}` → `{i['source']}` ({i['action']})" for i in report["links"]) + "\n")
    contents = {"NEXT_STEPS.md": note,
                "apply.sh": "#!/bin/sh\nset -eu\nexec " + command + " --apply\n",
                "verify.sh": "#!/bin/sh\nset -u\nhostname\nid -un\ncommand -v node || true\ncommand -v npm || true\nexec " + doctor + "\n"}
    # Refuse all known conflicts before creating even the first output file.
    for name in contents:
        if os.path.lexists(directory / name):
            raise FileExistsError(errno.EEXIST, "Export file exists", str(directory / name))
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        with (directory / name).open("x", encoding="utf-8") as stream:
            stream.write(content)
    return {"status": "EXPORTED", "directory": str(directory), "files": list(contents), "links_applied": False}


def show(report: dict, as_json: bool = False) -> None:
    if as_json or "internal_node" not in report:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"Status: {report['status']}")
    for label, item in (("Internal Node", report["internal_node"]), ("Companion npm", report["npm"])):
        check = item.get("probe", item.get("paired_path", {}))
        print(f"{label}: {item['entry']} => {item.get('resolved', 'unresolved')} [{check['status']}] {check.get('stdout', '')}")
    print(f"BioMind/Pi minimum Node: {'.'.join(map(str, MINIMUM_NODE))}; compatible: {report['internal_node']['compatible']}")
    print("Internal Node version probe: " + json.dumps(report["internal_node"]["probe"], ensure_ascii=False))
    print("Current PATH discovery (sanitized version tests): " + json.dumps(report["current_path"], ensure_ascii=False))
    print("npm in current PATH conditions: " + json.dumps(report["npm"]["current_path"], ensure_ascii=False))
    print("npm via selected Node: " + json.dumps(report["npm"]["via_node"], ensure_ascii=False))
    print("Bin directory: " + json.dumps(report["bin_directory"], ensure_ascii=False))
    print(f"Bin in current PATH: {report['bin_in_path']}; no shell changes will be made.")
    print("Receipt directory: " + json.dumps(report["receipt_directory"], ensure_ascii=False))
    print(f"Receipt: {report['receipt_path']}; phase: {(report['receipt'] or {}).get('phase', 'ABSENT')}")
    if report.get("receipt_error"):
        print("Receipt error: " + json.dumps(report["receipt_error"], ensure_ascii=False))
    for item in report["links"]:
        print(f"{item['action']}: {item['destination']} -> {item['source']}")
    print(report["impact"])
    print("Only temporary /tmp version probes are created and cleaned. Directory writability is NOT_VERIFIED.")
    if report['status'] in {'READONLY_HINT', 'RECEIPT_NEEDS_ATTENTION'}:
        print("Inspect this execution view and use the normal SSH handoff; no automatic retry or cleanup.")
    print("Ordinary SSH: NOT_RUN; Windows Orca version: UNKNOWN; Orca probe: NOT_RUN; relay: NOT_RUN.")


def confirm() -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input("Type yes to approve exactly these changes (anything else cancels): ").strip() == "yes"
    except (EOFError, KeyboardInterrupt):
        return False


def main(argv, *, runtime: Path) -> int:
    parser = argparse.ArgumentParser(description="Offline diagnosis / optional Orca user links; no installation or shell edits")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--integration", choices=["orca"], required=True)
    integration = commands.add_parser("integrate")
    integration.add_argument("integration", choices=["orca"])
    actions = integration.add_mutually_exclusive_group()
    actions.add_argument("--apply", action="store_true")
    actions.add_argument("--undo", action="store_true")
    actions.add_argument("--export-dir", type=Path)
    for command in (doctor, integration):
        command.add_argument("--pi-node", type=Path)
        command.add_argument("--bin-dir", type=Path, help="Explicit user bin directory; does not configure client discovery")
        command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    home = Path.home().absolute()
    bin_dir = (args.bin_dir.expanduser().absolute() if args.bin_dir else home / ".local/bin")
    refresh = lambda: diagnose(runtime, home, bin_dir, os.environ, args.pi_node)
    try:
        report = refresh()
        if args.command == "doctor":
            show(report, args.json)
            return 0 if (report["status"] == "NO_CHANGES_NEEDED" and report["internal_node"]["compatible"]
                         and report["current_path"]["compatible"]) else 1
        if args.export_dir:
            result = export_handoff(args.export_dir.expanduser().absolute(), report, args.pi_node, bin_dir)
        elif args.undo:
            plan = undo_plan(report)
            show(plan)
            if plan["status"] == "NO_OWNED_LINKS":
                return 0
            if plan["status"] != "UNDO_PREVIEW" or not confirm():
                result = {"status": "REFUSED", "reason": "CONFIRMATION_REQUIRED_OR_INVALID_RECEIPT"}
            else:
                result = undo_links(report, plan)
        else:
            show(report, args.json)
            if not args.apply:
                return 0 if report["status"] in {"READY_TO_APPLY", "NO_CHANGES_NEEDED"} else 1
            if report["status"] == "NO_CHANGES_NEEDED":
                return 0
            if report["status"] not in {"READY_TO_APPLY", "READONLY_HINT"} or not confirm():
                result = {"status": "REFUSED", "reason": "CONFIRMATION_REQUIRED_OR_BLOCKED_PLAN"}
            else:
                result = apply_links(report, refresh)
        show(result)
        return 0 if result["status"] in {"APPLIED", "UNDONE", "EXPORTED", "NO_CHANGES_NEEDED"} else 2
    except (OSError, ValueError, RuntimeError) as error:
        show({"status": "FAILED", "error": failure(error, "runtime_integration", Path(error.filename or bin_dir))
              if isinstance(error, OSError) else {"error_type": type(error).__name__}})
        return 2
