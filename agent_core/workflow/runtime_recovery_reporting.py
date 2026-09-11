"""Truthful, deterministic reporting for pre-generation runtime recovery only.

Reuses the task store's CAS writer and canonical output contracts. It never
changes plan/step terminal states, invokes a provider or grants execution.
Unlike the legacy blocked finalizer, report creation is opt-in and no-clobber.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from agent_core.plan_tracker import (
    canonical_task_spec_for_state, load_plan_state, save_plan_state,
    semantic_plan_hash, plan_approval_is_current,
)
from agent_core.runtime_evidence import evaluate_runtime_evidence
from agent_core.task_spec import effective_output_contracts
from agent_core.workflow.runtime_evidence_recovery import runtime_authorization_scope_digest

_REPORTS = frozenset({"report.md", "reproduce.sh", "stage_status.tsv"})
_CODE = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,240}$")
_SENSITIVE = re.compile(r"(?i)(?:secret|password|credential|authorization|api[_-]?key|(?:^|[/_.-])token(?:$|[/_.-])|(?:^|/)\.env(?:$|[./]))")
_FINISHED = frozenset({"done", "completed", "passed"})
_ATTEMPT_FINISHED = frozenset({"COMPLETED", "VALIDATION_PASSED", "VALIDATED"})
_VALIDATION_PASSED = frozenset({"valid", "valid_with_warnings", "passed", "pass", "success"})
_FAILURE_CLASSES = frozenset({"runtime_evidence_missing", "internal_protocol", "cancelled",
    "recovered_runtime_evidence", "dependency_unavailable", "authorization_unresolved",
    "internal_configuration", "inspection_authorization_unproven", "inspection_error", "authorization_required"})


class RuntimeReportError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _code(value, fallback="RUNTIME_RECOVERY_UNRESOLVED"):
    value = str(value or "")
    return value if _CODE.fullmatch(value) and (value in _FAILURE_CLASSES or not _SENSITIVE.search(value)) else fallback


def _text(value, limit=400):
    value = str(value or "").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return "[redacted]" if _SENSITIVE.search(value) else value[:limit]


def _path(value) -> Path:
    text = str(value or "")
    if not text.startswith("/") or _SENSITIVE.search(text) or any(ord(char) < 32 for char in text):
        raise RuntimeReportError("RUNTIME_REPORT_PATH_REJECTED")
    path = Path(text)
    if ".." in path.parts or any(part in {".ssh", ".aws", ".codex"} for part in path.parts):
        raise RuntimeReportError("RUNTIME_REPORT_PATH_REJECTED")
    if path.name in {"启动.sh", "startup.py", ".agent_history"}:
        raise RuntimeReportError("RUNTIME_REPORT_PATH_REJECTED")
    return path


def _open_directory(path: Path, *, create=False):
    """Open every component relative to a held fd, rejecting symlink parents."""
    path = _path(path)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _file_metadata(path: Path):
    descriptor = _open_directory(path.parent)
    try:
        info = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            return None
        return {"size": info.st_size, "mtime_ns": info.st_mtime_ns}
    finally:
        os.close(descriptor)


def _write_report(path: Path, body: str, *, prior_receipt: Mapping[str, Any] | None = None):
    """Publish a fully fsynced new file atomically; never replace unknown files."""
    path = _path(path)
    if path.name not in _REPORTS:
        raise RuntimeReportError("RUNTIME_REPORT_FILENAME_REJECTED")
    content = body.encode("utf-8")
    if len(content) > 1024 * 1024:
        raise RuntimeReportError("RUNTIME_REPORT_SIZE_LIMIT")
    fingerprint = hashlib.sha256(content).hexdigest()
    parent = _open_directory(path.parent, create=True)
    temporary = ".runtime-recovery-" + uuid.uuid4().hex + ".tmp"
    created = False
    try:
        try:
            existing = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            prior = dict(prior_receipt or {})
            if not stat.S_ISREG(existing.st_mode) or prior.get("sha256") != fingerprint or prior.get("path") != str(path) or existing.st_size != len(content):
                raise RuntimeReportError("RUNTIME_REPORT_EXISTING_FILE_PROTECTED")
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as handle:
                if hashlib.sha256(handle.read(len(content) + 1)).hexdigest() != fingerprint:
                    raise RuntimeReportError("RUNTIME_REPORT_OWNED_FILE_CHANGED")
            return {"path": str(path), "sha256": fingerprint}
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        except FileExistsError as exc:
            # A concurrent publisher owns the destination. Preserve it and
            # report the same protected-file condition as the initial check.
            raise RuntimeReportError("RUNTIME_REPORT_EXISTING_FILE_PROTECTED") from exc
        os.unlink(temporary, dir_fd=parent)
        created = False
        os.fsync(parent)
        return {"path": str(path), "sha256": fingerprint}
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def _current_scope_matches(durable, request):
    scope = dict(request.get("scope") or {})
    recovery = dict((durable.get("task_state") or {}).get("runtime_recovery") or {})
    if recovery.get("active_episode_id") != request.get("episode_id"):
        return False
    stored = (recovery.get("episodes") or {}).get(request.get("episode_id")) or {}
    if stored.get("request") != request:
        return False
    if any(str(scope.get(key) or "") != str(durable.get(key) or "") for key in ("task_id", "plan_id", "plan_version")):
        return False
    if str(scope.get("step_id") or "") != str(durable.get("current_step_id") or ""):
        return False
    if scope.get("semantic_plan_hash") != semantic_plan_hash(durable):
        return False
    current_revision = int(durable.get("task_revision") or (durable.get("current_task") or {}).get("task_revision") or 0)
    if int(request.get("task_revision_at_creation") or 0) != current_revision:
        return False
    if scope.get("authorization_scope_digest") != runtime_authorization_scope_digest(durable):
        return False
    return True


def _durable_facts(durable, request):
    completed, unfinished, scripts = [], [], []
    provenance = dict(durable.get("provenance_state") or {})
    attempts = dict(provenance.get("attempts") or {})
    validations = dict(provenance.get("validations") or {})
    for step in durable.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        step_id = _code(step.get("step_id"), "unknown_step")
        successful = []
        for attempt_id, attempt in attempts.items():
            if not isinstance(attempt, Mapping) or attempt.get("step_id") != step.get("step_id"):
                continue
            validation = validations.get(attempt.get("validation_evidence_ref")) or {}
            if attempt.get("returncode") == 0 and str(attempt.get("status") or "").upper() in _ATTEMPT_FINISHED and (str(validation.get("status") or "").lower() in _VALIDATION_PASSED or str(attempt.get("status") or "").upper() == "COMPLETED"):
                successful.append(_code(attempt_id, "accepted_attempt"))
                script = str(attempt.get("executed_script_path") or "")
                if script:
                    try:
                        path = _path(script)
                        if _file_metadata(path):
                            scripts.append({"step_id": step_id, "attempt_id": _code(attempt_id), "script_path": str(path)})
                    except (OSError, ValueError):
                        pass
        done = str(step.get("status") or "").lower() in _FINISHED and bool(successful)
        dispatched = any(isinstance(attempt, Mapping) and attempt.get("step_id") == step.get("step_id") and (attempt.get("execution_record_id") or str(attempt.get("status") or "").upper() in {"RUNNING", "PROCESS_EXITED", "EVIDENCE_COMMITTED", *_ATTEMPT_FINISHED}) for attempt in attempts.values())
        row = {"step_id": step_id, "title": _text(step.get("title")),
               "status": "completed" if done else "executed_unverified" if dispatched else "not_dispatched",
               "accepted_attempt_ids": successful}
        (completed if done else unfinished).append(row)
    completed_ids = {row["step_id"] for row in completed}
    deliveries = []
    for artifact_id, version_id in (provenance.get("artifact_heads") or {}).items():
        artifact = (provenance.get("artifacts") or {}).get(version_id) or {}
        if artifact.get("producer_step_id") not in completed_ids or str(artifact.get("status") or "").lower() != "valid" or str(artifact.get("validation_status") or "").lower() not in _VALIDATION_PASSED:
            continue
        attempt = attempts.get(artifact.get("producer_attempt_id")) or {}
        if attempt.get("step_id") != artifact.get("producer_step_id") or attempt.get("returncode") != 0:
            continue
        try:
            path = _path(artifact.get("path"))
            metadata = _file_metadata(path)
            signature = dict(artifact.get("digest_or_signature") or {})
            if not metadata or not signature or any(signature.get(key) != metadata[key] for key in ("size", "mtime_ns")):
                continue
        except (OSError, ValueError):
            continue
        deliveries.append({"artifact_id": _code(artifact_id, "accepted_artifact"), "artifact_version_id": _code(version_id, "accepted_version"),
                           "path": str(path), "producer_step_id": artifact["producer_step_id"],
                           "verification": "durably_accepted_current_metadata_matches"})
    required = list((request.get("scope") or {}).get("required_binding_ids") or request.get("required_binding_ids") or [])
    runtime = evaluate_runtime_evidence(durable.get("task_state") or {}, task_id=str(durable.get("task_id") or ""),
        skill_scope=(request.get("scope") or {}).get("subject") or {}, required_binding_ids=required)
    return completed, unfinished, deliveries, scripts, runtime


def _requested_reports(durable, task_root):
    spec = canonical_task_spec_for_state(durable)
    outputs = effective_output_contracts(spec)
    approved = plan_approval_is_current(durable, int((durable.get("current_task") or {}).get("task_revision") or durable.get("task_revision") or 0))
    paths = {}
    for row in outputs:
        raw = str(row.get("canonical_path") or row.get("filename") or "")
        if Path(raw).name not in _REPORTS or row.get("artifact_kind", "file") != "file":
            continue
        path = Path(raw)
        if path.is_absolute():
            path = _path(path)
            if not path.is_relative_to(task_root) and not (approved and str(row.get("source") or row.get("source_type")) in {"user_explicit", "user_confirmed"} and row.get("path_policy") == "managed_exact"):
                raise RuntimeReportError("RUNTIME_REPORT_EXPLICIT_OUTPUT_NOT_APPROVED")
        else:
            if path.parts != (path.name,):
                raise RuntimeReportError("RUNTIME_REPORT_RELATIVE_PATH_REJECTED")
            path = task_root / path.name
        # Even an approved output may not alias a declared scientific input.
        input_paths = {str(item.get("canonical_path") or item.get("path") or "") for item in spec.get("inputs") or [] if isinstance(item, Mapping)}
        if str(path) in input_paths:
            raise RuntimeReportError("RUNTIME_REPORT_INPUT_OVERLAP")
        if path.name in paths and paths[path.name] != path:
            raise RuntimeReportError("RUNTIME_REPORT_OUTPUT_AMBIGUOUS")
        paths[path.name] = path
    return paths


def _explanation(summary):
    status = summary["resolution_disposition"]
    leading = {
        "unresolved": "当前步骤的运行证据仍不足，自动恢复已停止；这不能证明依赖不存在。",
        "dependency_blocked": "有效且完整的运行检查已确认声明范围内至少一个必需运行条件不满足。",
        "needs_user_decision": "当前步骤尚未获得新的执行许可；继续需要用户处理明确的选择或授权。",
        "internal_error": "运行证据协议、检查或存储出现内部问题，尚不能据此判断依赖是否可用。",
        "stale_scope": "本次恢复对应的计划或步骤范围已经变化；仅保留旧 episode 诊断，不更新当前任务结果。",
        "evidence_ready": "运行证据已经持久化；接下来只恢复当前步骤的 Generate，并继续原有审批及执行前检查。",
    }[status]
    lines = [leading, "科学目标完成：否。", "原始错误：" + summary["original_error_code"] + "；停止原因：" + summary["reason_code"] + "。"]
    if summary["request_status"] == "unrecorded_request":
        lines.append("恢复请求未能持久保存；本次返回故障说明，不更新现有恢复记录或生成报告文件。")
    lines.append("已完成并验收的阶段：" + ("；".join(row["step_id"] + "（" + row["title"] + "）" for row in summary["completed_stages"]) or "暂无可核验记录") + "。")
    lines.append("未完成阶段：" + ("；".join(row["step_id"] + "=" + row["status"] for row in summary["unfinished_stages"]) or "当前无法可靠枚举") + "。")
    lines.append("已验证交付物：" + ("；".join(row["path"] for row in summary["verified_deliverables"]) or "暂无可核验交付物") + "。")
    lines.append("当前运行事实：" + ("；".join(binding + "=" + fact["availability"] for binding, fact in summary["per_binding_status"].items()) or "UNKNOWN（没有可信事实）") + "。")
    lines.append("实际恢复操作：" + ("；".join(row["method"] + "=" + row["inspection_status"] + "（" + row["reason_code"] + "）" for row in summary["attempted_recovery"]) or "没有已记录的检查") + "。")
    lines.append("继续条件：" + summary["next_condition"])
    return "\n\n".join(lines)


def finalize_runtime_recovery_result(data_path, task_id, result, *, state=None, defer_reports=False):
    """Always return complete user text, including after durable/report failure."""
    failures, report_paths, receipts = [], {}, {}
    request = deepcopy(dict(result.get("request") or {}))
    episode_id = _code(request.get("episode_id"), "unrecorded_episode")
    unrecorded = not request.get("episode_id") or not request.get("request_id") or not request.get("scope")
    supplied = dict(result.get("explanation") or {}) if isinstance(result.get("explanation"), Mapping) else {}
    status = str(result.get("status") or "internal_error")
    if status not in {"unresolved", "dependency_blocked", "needs_user_decision", "internal_error", "evidence_ready"}:
        status = "internal_error"
    reason = _code(result.get("reason_code"))
    summary = {"schema_version": 1, "producer": "runtime_recovery_reporting_v1", "episode_id": episode_id,
        "request_status": "unrecorded_request" if unrecorded else "recorded_request",
        "scope": {key: _code((request.get("scope") or {}).get(key), "") for key in ("task_id", "plan_id", "plan_version", "step_id")},
        "failure_class": _code((result.get("episode") or {}).get("failure_class") or supplied.get("failure_class"), "internal_protocol"),
        "resolution_disposition": status, "scientific_goal_completed": False, "execution_status": "unknown",
        "original_error_code": _code(request.get("original_error_code") or supplied.get("original_error_code")),
        "reason_code": reason, "completed_stages": [], "unfinished_stages": [], "verified_deliverables": [],
        "per_binding_status": {}, "attempted_recovery": [], "report_paths": {}, "summary_mode": "deterministic_fallback", "summary_llm_calls": 0}
    durable, matching, scripts = None, False, []
    try:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}", str(task_id)):
            raise RuntimeReportError("RUNTIME_REPORT_TASK_ID_REJECTED")
        if unrecorded:
            # Request admission/storage failed before a durable episode existed.
            # This is not a changed-plan episode and owns no report/store writes.
            raise RuntimeReportError("RUNTIME_REPORT_REQUEST_UNRECORDED")
        task_root = _path(Path(data_path).absolute() / "task_outputs" / str(task_id))
        if not _file_metadata(task_root / "plan_state.json"):
            raise RuntimeReportError("RUNTIME_REPORT_DURABLE_STATE_UNREADABLE")
        durable = load_plan_state(data_path, task_id)
        if durable.get("load_error") or durable.get("canonical_state_error"):
            raise RuntimeReportError("RUNTIME_REPORT_DURABLE_STATE_UNREADABLE")
        matching = _current_scope_matches(durable, request)
        if not matching:
            summary["resolution_disposition"] = "stale_scope"
            failures.append({"stage": "scope", "reason_code": "RUNTIME_REPORT_SCOPE_CHANGED"})
        else:
            completed, unfinished, deliveries, scripts, runtime = _durable_facts(durable, request)
            summary.update(completed_stages=completed, unfinished_stages=unfinished,
                verified_deliverables=deliveries, per_binding_status=runtime)
            current_stage = next((row for row in completed + unfinished if row["step_id"] == (request.get("scope") or {}).get("step_id")), {})
            if current_stage.get("status") in {"completed", "executed_unverified"}:
                summary["execution_status"] = "existing_execution_record"
            elif current_stage.get("status") == "not_dispatched":
                summary["execution_status"] = "not_dispatched"
            stored_episode = ((durable.get("task_state") or {}).get("runtime_recovery") or {}).get("episodes", {}).get(request["episode_id"], {})
            summary["attempted_recovery"] = [{"method": _code(row.get("method_id"), "restore_or_inspection"),
                "inspection_status": _code(row.get("inspection_status") or row.get("status"), "not_run"),
                "reason_code": _code(row.get("reason_code"), "none"), "progress": row.get("progress") is True}
                for row in stored_episode.get("receipts") or [] if isinstance(row, Mapping)]
            if status == "needs_user_decision":
                question = _text((stored_episode.get("user_decision") or {}).get("question"), 1000)
                if question and question != "[redacted]":
                    summary["user_question"] = question
                else:
                    summary["resolution_disposition"] = "unresolved"
                    failures.append({"stage": "decision", "reason_code": "RUNTIME_REPORT_USER_QUESTION_MISSING"})
            if status == "dependency_blocked" and not any(fact["availability"] == "UNAVAILABLE" for fact in runtime.values()):
                summary["resolution_disposition"] = "unresolved"
                failures.append({"stage": "evidence", "reason_code": "RUNTIME_REPORT_NEGATIVE_NOT_VERIFIED"})
    except Exception as exc:
        durable = None
        summary["resolution_disposition"] = "internal_error"
        failures.append({"stage": "request" if unrecorded else "read", "reason_code": _code(getattr(exc, "code", None), "RUNTIME_REPORT_STATE_READ_FAILED"), "error_type": type(exc).__name__})
    next_conditions = {
        "unresolved": "取得与当前 Skill、环境及检查契约一致的新证据，修复检查错误或缺失的证据生产器后，再恢复同一步；预算耗尽不代表依赖缺失。",
        "dependency_blocked": "由用户或维护者处理已确认不满足的运行条件；取得新的可信证据并重新通过原有审批后，再恢复同一步。不会自动安装、下载或切换环境。",
        "needs_user_decision": "处理原有审批流程中明确列出的选择或授权；未回答前不执行依赖步骤。",
        "internal_error": "修复记录中的协议、检查或持久化错误，然后重新验证当前范围的证据；不得将内部错误当作依赖不存在。",
        "stale_scope": "由当前计划与步骤范围重新获取恢复请求；旧请求不授权新的执行。",
        "evidence_ready": "重试同一步 Generate；审批、manifest、workspace 和 Execute prelaunch 仍必须通过。",
    }
    summary["next_condition"] = next_conditions[summary["resolution_disposition"]]
    if summary["resolution_disposition"] == "unresolved" and reason == "RUNTIME_USER_ANSWER_WITHOUT_SCOPE_CHANGE":
        summary["next_condition"] = "已收到用户答复，但当前运行证据及有效授权范围尚未变化；需要将具体选择落实到受控证据或现有审批记录后，再恢复同一步。不会重复提问或据此执行。"
    if summary.get("user_question"):
        summary["next_condition"] = summary["user_question"] + " 未回答前不执行依赖步骤。"
    explanation = _explanation(summary)
    if durable is not None and matching and not defer_reports:
        try:
            targets = _requested_reports(durable, task_root)
            prior = dict((durable.get("task_state") or {}).get("runtime_recovery_report_receipts") or {})
            stage_rows = ["stage\tstatus\toutput_path\tnotes"]
            for row in summary["completed_stages"] + summary["unfinished_stages"]:
                paths = [item["path"] for item in summary["verified_deliverables"] if item["producer_step_id"] == row["step_id"]]
                stage_rows.append("\t".join((row["step_id"], row["status"], ";".join(paths) or "-", row["title"])))
            stage_rows.append("task\tincomplete\t-\t" + summary["reason_code"])
            script = ["#!/bin/sh", "# BioMind incomplete run: evidence references only; no scientific replay is authorized.", "# Runtime recovery episode: " + episode_id]
            script += ["# Accepted step " + row["step_id"] + "; attempt " + row["attempt_id"] + "; script reference: " + row["script_path"] for row in scripts]
            script += ["# Not completed: " + row["step_id"] + " (" + row["status"] + ")" for row in summary["unfinished_stages"]]
            script += ["# Continue only after the runtime evidence and original approval/prelaunch checks pass.", "exit 1"]
            bodies = {"report.md": "# BioMind runtime recovery status\n\n" + explanation + "\n", "stage_status.tsv": "\n".join(stage_rows) + "\n", "reproduce.sh": "\n".join(script) + "\n"}
            for name, path in targets.items():
                try:
                    fresh = load_plan_state(data_path, task_id)
                    if not _current_scope_matches(fresh, request):
                        raise RuntimeReportError("RUNTIME_REPORT_SCOPE_CHANGED")
                    receipt = _write_report(path, bodies[name], prior_receipt=prior.get(str(path)))
                    receipts[str(path)] = {**receipt, "episode_id": episode_id}
                    report_paths[name] = str(path)
                except Exception as exc:
                    failures.append({"stage": "report", "report_name": name, "reason_code": _code(getattr(exc, "code", None), "RUNTIME_REPORT_WRITE_FAILED"), "error_type": type(exc).__name__})
        except Exception as exc:
            failures.append({"stage": "report_contract", "reason_code": _code(getattr(exc, "code", None), "RUNTIME_REPORT_CONTRACT_FAILED"), "error_type": type(exc).__name__})
    summary["report_paths"] = report_paths
    summary["secondary_failures"] = deepcopy(failures)
    summary["stage"] = "runtime_evidence_recovery"
    summary["original_error"] = {"error_code": summary["original_error_code"]}
    summary["explanation"] = explanation + ("\n\n附加问题：" + "；".join(row["reason_code"] for row in failures) + "。以上文字说明仍可使用。" if failures else "")
    # Reload immediately before CAS. A stale report remains an old-episode
    # diagnosis; it cannot replace a newer active result or change any step.
    if durable is not None:
        try:
            current = load_plan_state(data_path, task_id)
            task = deepcopy(dict(current.get("task_state") or {}))
            if _current_scope_matches(current, request):
                task["runtime_recovery_result"] = deepcopy(summary)
                task["runtime_recovery_report_receipts"] = {**dict(task.get("runtime_recovery_report_receipts") or {}), **receipts}
            else:
                old = deepcopy(dict(task.get("runtime_recovery_diagnostics") or {}))
                old[episode_id] = deepcopy(summary)
                task["runtime_recovery_diagnostics"] = old
            current["task_state"] = task
            save_plan_state(data_path, task_id, current)
        except Exception as exc:
            failures.append({"stage": "persist", "reason_code": _code(getattr(exc, "code", None), "RUNTIME_REPORT_RESULT_PERSIST_FAILED"), "error_type": type(exc).__name__})
    if failures:
        explanation += "\n\n附加问题：" + "；".join(row["reason_code"] for row in failures) + "。以上文字说明仍可使用。"
    summary["secondary_failures"] = deepcopy(failures)
    summary["explanation"] = explanation
    summary["stage"] = "runtime_evidence_recovery"
    summary["original_error"] = {"error_code": summary["original_error_code"]}
    return {"explanation": explanation, "runtime_recovery_result": summary, "report_paths": report_paths, "secondary_failures": failures,
            "reason_code": summary["reason_code"], "resolution_disposition": summary["resolution_disposition"],
            "execution_status": summary["execution_status"], "original_error": summary["original_error"], "stage": "runtime_evidence_recovery"}
