# Recovery Prompt Composition

Audit date: 2026-07-31

This document follows prompt construction through the final `ainvoke()` call.
It does not infer content merely because a template exists.

## Generate Recovery Call

Final call (`bio_agents.py:11113-11121`):

```python
call_msgs = [
    SystemMessage(content=stable_sys + "\n\n" + ephemeral_context),
    *visible_messages,
]
response = await self._ainvoke_llm(call_msgs, stage="generate", ...)
```

### Message 1: Stable System Plus Ephemeral Context

| Component | Present | Source | Bound | Structured / possibly empty |
|---|---|---|---:|---|
| System header and prime directives | Yes | stable prompt builder, `bio_agents.py:1148-1202` | Session-cached; no per-call clipping here | Text; non-empty |
| Tool/runtime interaction protocol | Yes | same stable builder | Part of stable prompt | Text |
| User preferences / AGENTS guidance | Yes when configured | same stable builder | Part of stable prompt | Text; may be empty |
| TaskSpec goal and bindings | Yes | `_generate_task_spec_projection()`, `bio_agents.py:2165-2199` | Goal 3k; resolved inputs first 24 | JSON |
| Complete Plan | No | `_current_step_prompt_context()`, `bio_agents.py:3891-3953` | Current step only | Structured text |
| Current step | Yes, more than once | TaskSpec projection, current-step block, plan progress/current plan | Ephemeral budget | JSON/text |
| Active Skill | Yes | `SkillManager.load_skills_for_prompt()`, `bio_agents.py:10897-10928` | Shared ephemeral budget, not a guaranteed short summary | Usually full selected skill text |
| Runtime audit | Yes | `_generate_runtime_audit_context()`, `bio_agents.py:2090-2135` | 3k chars | Structured text |
| Renderer protocol/schema | Yes for Renderer steps | `_current_action_contract()`, `bio_agents.py:3713-3779` and Renderer protocol | Ephemeral budget | Structured JSON/text |
| Formal verified inputs | Yes when TaskSpec/audit populated | current action contract and projected Renderer args | Ephemeral budget | JSON |
| Manifest state | Yes for Renderer steps | current action contract / renderer execution state | Ephemeral budget | JSON |
| RecoveryDecision | Yes in recovery | `bio_agents.py:10973-11006` | PATCH_SCRIPT compact decision; otherwise recovery payload max 5k | JSON |
| DiagnosticContext | Partial | repair-generator context | PATCH_SCRIPT projection reduced to 3k | JSON summary |
| Previous attempts | Yes in recovery payload | last two recovery attempts | Inside 5k payload | JSON |
| Recovery progress | Optional | `state.recovery_progress` | Inside 5k payload | Often empty |
| ValidationEvidence | Not directly guaranteed | not a pinned Generate block | Only indirect summaries/messages | Usually absent as a full object |
| stdout/stderr | Partial, never intentionally full | latest selected observation and DiagnosticContext | per-message/diagnostic bounds | Text/path summary |
| Artifact snapshot | Conditional | DiagnosticContext or Renderer state | Diagnostic projection | JSON; may be absent outside PATCH_SCRIPT |
| RAG / memory / archive | Conditional | `_generate_on_demand_memory_contexts()`, `bio_agents.py:1466-1545` | Low-priority/droppable ephemeral blocks | Text summaries |
| Budgets | Partial | recovery payload/lifecycle-derived fields | Small | Not a complete typed budget snapshot |

`PromptManager.build_ephemeral_context_result()` enforces a 26k-character
ephemeral budget (`prompts/manager.py:226-375`). Pinned blocks receive minimum
budgets; memory/RAG blocks are lower priority and can be dropped.

### Messages 2-N: Step-Local Visible Messages

`_generate_visible_messages()` delegates to `_step_local_messages()`
(`bio_agents.py:3782-3889`). It does not pass the complete graph history.
Selection is bounded to six messages and 12k characters and normally includes:

1. first user goal, unless removed because TaskSpec already carries the same
   goal;
2. latest human answer;
3. latest execution observation;
4. latest control/recovery message;
5. latest AI action.

The original user request commonly survives as the TaskSpec projection rather
than as a verbatim HumanMessage. Consultant internal exchanges are excluded.

One gap is that `<validation_evidence>` is not among the explicit
control-message markers used by step-local selection. Full ValidationEvidence
can therefore disappear, while a much smaller compatibility
`last_supervisor_feedback` remains.

### Action-Format Repair

Format repair uses the same Generate call. `_action_format_repair_feedback()`
injects:

- why the previous action was invalid;
- current step ID;
- dynamically allowed actions;
- canonical Renderer ID and argument schema when applicable;
- verified inputs and projected arguments;
- manifest status;
- a demand for exactly one structured action.

The raw invalid response is persisted under `action_protocol/`, with a hash.
The next prompt changes; a separate repair LLM is not called.

## RecoveryJudge Call

Active call (`agent_core/middleware.py:1712-1730`):

```text
HumanMessage(
  RECOVERY_JUDGE_PROMPT
  + bounded DiagnosticContext JSON
)
```

DiagnosticContext composition (`agent_core/diagnostics.py:665-835`):

- original task goal, TaskSpec inputs/output/constraints;
- current Plan step and success criteria, not full Plan;
- action/step IDs and command role;
- approved script/source and failed line/command;
- bounded stdout/stderr plus full-stream paths;
- expected and actual runtime;
- Renderer manifest/action information;
- artifact snapshot and validator results;
- Supervisor and CompletionGate summaries;
- up to three recent attempts;
- active-skill recovery policy;
- deterministic diagnosis and missing evidence.

Default limits (`agent_core/diagnostics.py:66-77`) are depth 4, 64 files,
128 KiB per text file, 1 MiB total reads, 100 log-tail lines, 80 error hits and
a 26k prompt view. Binary bioinformatics files are treated as metadata.

Schema repair receives the original Judge prompt plus the parse exception, but
does not include the complete raw invalid Judge response. That makes exact
self-correction weaker than Generate's action-format repair.

## Supervisor Scientific Advisory

Active call (`supervisor.py:512-586`) is one HumanMessage:

```text
strict JSON instructions
+ latest code (first 1,500 chars)
+ projected stdout (up to 3,000 chars)
+ stderr (up to 1,200 chars)
+ task context (up to 1,500 chars)
```

It does not receive complete TaskSpec, Plan, recovery history, manifest or
RecoveryDecision. This is intentional because it is advisory, not a router.
One schema repair call receives the first prompt plus a repair HumanMessage.

## Planner Repair Call

Planner calls go through the central `_ainvoke_llm()` wrapper
(`bio_agents.py:10255,10316`). A local repair receives the approved plan
projection, TaskSpec/selected skill/runtime/formal input context and structured
PLAN-owned feedback. It should not receive an ordinary SCRIPT/RUNTIME failure,
because the controller rejects Planner routing without `PLAN_PATCH`.

## Installer Recovery Call

Installer final array (`agent_core/installer.py:492-507`):

1. Installer SystemMessage;
2. HumanMessage work order;
3. optional RAG SystemMessage;
4. selected latest failed observation;
5. selected latest AI installer response;
6. selected latest SystemMessage.

The selection is bounded to six messages / 12k characters
(`agent_core/installer.py:119-183`). Failed command observations expose only
about 500 characters each of stdout and stderr. Installer does not receive the
main TaskSpec, DiagnosticContext, ValidationEvidence, CompletionGate or
RecoveryDecision schema.

## `RECOVERY_TEMPLATE`

`RECOVERY_TEMPLATE` is defined at `prompts/templates.py:323-337` and is still
conditionally injected by `PromptManager.build_ephemeral_context_result()` at
`prompts/manager.py:247-251`. It is therefore not strictly dead.

Its trigger checks whether the final item of the supplied history contains both
`<observation>` and `❌`. In the active path, middleware commonly appends a
`<structured_recovery>` SystemMessage after the failed observation, and
Supervisor may append ValidationEvidence later. The failed observation is then
not the final history item, so the template frequently does not fire.

When it does fire, it overlaps with the structured recovery block and can
duplicate “do not repeat / inspect the error” guidance. It is an unreliable
legacy compatibility prompt, not the authoritative recovery instruction.

## Prompt Size Ceiling

Generate is not sent all 200k graph memory. Its practical upper composition is:

```text
stable system prompt (session-specific, about 12k in the replay)
+ ephemeral context (maximum 26k)
+ selected visible messages (maximum 12k)
≈ 50k characters
```

Memory compression may alter graph messages at >=85% usage
(`bio_agents.py:8608-8636`), but Generate already uses a current-step
projection. The remaining risk is duplicated current-step/TaskSpec/action
content inside the bounded prompt, not wholesale history replay.

