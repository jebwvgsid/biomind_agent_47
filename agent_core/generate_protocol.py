"""The same output instructions for initial, corrective and local Generate."""
import re

SCRIPT_OUTPUT_PROTOCOL = r'''Return exactly one of these alternatives:
1. One complete raw JSON display envelope:
{"response_type":"biomind_generate_display_v1","public_message":"候选已形成，等待检查与审批。","payload":"<shell>\nprintf 'fixture\\n'\n</shell>"}
The decoded payload contains exactly one non-empty executable block:
<shell>
# actual Bash script
</shell>
or <python>...</python> or <r>...</r>.
Use a bare tag, optionally with skill_resources="selected/skill#scripts/file.py"
(semicolon-separated for several handles).
An admitted retry_step keeps the same action's selected helper resources when
this attribute is omitted. To change helpers, explicitly name the new handles;
to switch to ordinary code, use skill_resources="". No old environment or
attempt directory is inherited; bindings are rebuilt before approval.
Do not add language attributes, CDATA, or explanation outside the block. Put explanation in
public_message. A single whole unlabelled/json Markdown fence containing the
complete JSON object is accepted as a display wrapper; prose outside it,
multiple fences and multiple JSON objects are not accepted.
Preserve the exact script whitespace, quoting, newlines and heredocs. A single
bare executable block is also accepted for compatibility.

2. One complete JSON request for a published registered helper:
{"type":"skill_script","handle":"selected/skill#scripts/file.py","arguments":["--flag","actual value"]}
Use only a handle from selected_skill_script_bindings. The program constructs
the interpreter and script argv before approval; supply only actual business
argument strings, never an interpreter, shell command, or guessed install root.
For a flag declared runtime_environment_variable, its value is exactly the
object {"runtime_binding":"THE_PUBLISHED_VARIABLE"}; it is bound only by the
execution environment after approval. Never invent its value or directory.
public_message is optional. This forms a candidate; it does not run the helper.
This request belongs at the top level, never in a display envelope's payload.

3. One raw typed JSON decision under the selected Skill decision protocol.

4. One complete JSON stop declaration:
{"type":"blocked","reason":"Exact unresolved problem, attempted correction, and evidence needed to continue."}
Use these two business fields and optional public_message. The non-empty
reason is at most 8000 characters. Do not put prose around this JSON. This
stops automatic work without completing the step. Do not emit <finish>, a
success claim, or code with it. If no substantive correction or new evidence
remains, submit blocked instead of changing comments to spend another attempt.
Do not assert a validator defect or missing dependency without evidence.

5. One dependency preparation request:
{"action_type":"install_tool","tool":"declared tool","env":"declared environment"}
This asks the Installer for an exact proposal and its existing approval. Inspect
usable environments first; unknown facts do not prove missing software. Do not
embed installation in the scientific script.

The display envelope permits only response_type, public_message and payload.
Never emit competing actions or multiple executable blocks. Describe the
candidate as formed and awaiting checks/approval. Generation does not execute
tools or prove inputs passed. Cite earlier observations with their actual scope.
Use # BIOMIND_EFFECT_HELPER exactly once if the published emission contract
requires it. Never copy framework-expanded serializer definitions from execution
evidence into a new candidate; the program materializes the current template.
Scientific computation and actual observed facts remain the model's responsibility.
'''


def generate_wire_contract(*, script_protocol: bool) -> dict:
    return {
        "protocol": "script_block_v1" if script_protocol else "native_action_json",
        "instructions": SCRIPT_OUTPUT_PROTOCOL if script_protocol else (
            "Return exactly one complete raw JSON action with action_type, or the "
            "complete blocked JSON. A single whole unlabelled/json fence is also accepted. "
            "No outside prose, action_json wrapper or competing objects. "
            "Use the current published action schema and binding requirements."
        ),
        "shell_example": "<shell>\n# actual approved computation\n</shell>" if script_protocol else None,
        "blocked_example": {"type" if script_protocol else "action_type": "blocked",
                            "reason": "Exact unresolved requirement and attempted correction."},
    }


def skill_script_example(request: dict) -> dict:
    """Show the existing action schema without guessing business arguments."""
    return {"type": "skill_script", "handle": request.get("handle", "selected/skill#scripts/file.py"),
        "arguments": request.get("arguments", [])}


def repair_source_view(evidence: dict) -> dict:
    """Keep materialized serializer code out of the default editing basis."""
    source = evidence.get("model_authored_code")
    if isinstance(source, str) and source:
        return {"model_authored_code": source, "code_basis": "model_draft_before_framework_materialization"}
    legacy = str(evidence.get("generated_code") or "")
    if legacy and not re.search(r"(?m)^\s*def\s+(?:emit_effect_evidence|_emit_declared_rows)\s*\(", legacy):
        return {"model_authored_code": legacy, "code_basis": "legacy_generated_source_may_be_excerpt"}
    return {"model_authored_code": "", "code_basis": "draft_unavailable_inspect_original_generate_response"}


def controlled_generate_payload(raw: str, *, managed: bool) -> str:
    """A plain diagnostic is an explicit stop, never evidence of completion.

    Structured candidates and completion declarations keep their existing
    parser/admission gates. Malformed structured input remains repairable.
    """
    import json
    text = str(raw or '').strip()
    if managed and text and not text.startswith(('{', '[', '<', '```')):
        return json.dumps({'type': 'blocked', 'reason': (
            'No explicit executable or evidence-backed completion candidate was submitted. '
            'Saved diagnostic: ' + text)[:8000]}, ensure_ascii=False)
    return raw
