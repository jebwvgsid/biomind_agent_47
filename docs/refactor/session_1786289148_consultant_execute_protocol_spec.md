# Session 1786289148 Consultant Execute Protocol Repair

Status: Implemented and regression-tested  
Incident: `session_1786289148`  
Scope: Consultant discovery-to-FINALIZE handoff; no scientific workflow changes

## 1. Decision

The incident was an internal Consultant action-protocol failure. It was not a
context-window overflow, a user ambiguity, a Wiki routing decision, or a failed
snpEff analysis command.

The controller must own skill-load confirmation, runtime verification, evidence
ledger updates, FINALIZE entry, and error routing. The model may request one
bounded read-only command, but it may not define an execution graph or emit its
own tool result.

## 2. Reconstructed failure chain

1. TRIAGE routed the task to Consultant.
2. Consultant selected and successfully loaded
   `genomics/snpeff-annotation`.
3. Consultant returned `action_type=execute` with `command_groups` instead of
   the required top-level `code` field.
4. The action parser rejected both repeated responses, but its generic repair
   text did not identify the invalid field or provide the exact accepted
   execute object.
5. The model then emitted a fabricated `<tool_result>` saying that no execute
   calls were configured. No controller EXECUTE had occurred; the durable
   `execute_call_count` remained zero.
6. The loop classifier searched for older error wording and did not reliably
   count the current `supported action format` failure.
7. The exhausted loop guard treated the internal protocol failure as user
   ambiguity and routed to `ask_user`/terminal projection. Planner was never
   entered. No durable Wiki node was executed.

The recorded Consultant prompt used approximately 28k tokens against the
configured 200k context limit. Token capacity was therefore not the cause.

## 3. Required invariants

1. Consultant `execute` accepts one top-level `code` string only.
2. `command_groups`, `commands`, `runner`, and `env` are not Consultant action
   fields and must never be silently flattened.
3. `<tool_result>` is controller-owned evidence. A model-emitted copy is a
   typed protocol error and is never counted as execution.
4. A skill is considered loaded only after a successful controller
   `<loaded_skill_doc>`, `<loaded_skill_reference>`, or
   `<loaded_skill_script>` observation.
5. Once a canonical selected skill is verified, the controller runs the
   declared runtime preflight and seeds its structured result into the
   consultation evidence ledger.
6. Fresh ready runtime evidence may upgrade an existing unresolved runtime
   requirement on resume.
7. When all mandatory requirements are satisfied, the controller enters
   FINALIZE before asking the model for another discovery action.
8. Format-repair exhaustion, fake evidence, runtime failure, and load failure
   are internal failures. They clear `pending_human_question` and never route
   to `ask_user`.
9. `ask_user` remains valid only for a schema-valid, provenance-bound semantic
   ambiguity that the controller cannot resolve.
10. FINALIZE context validation must accept the immutable tuple projection
    produced by its own context builder while continuing to compare the thawed
    canonical constraints strictly.

## 4. Action ABI

Accepted read-only execution request:

```json
{
  "action_type": "execute",
  "requirement_ids": ["runtime:genomics_snpeff-annotation"],
  "purpose": "verify the declared snpEff runtime",
  "expected_evidence": {
    "RESOLVED_SNPEFF_JAR": "absolute path",
    "RESOLVED_JAVA_HOME": "absolute path"
  },
  "language": "bash",
  "code": "one bounded read-only shell script"
}
```

Rejected shapes receive `ACTION_FORMAT_INVALID` with a field-specific repair.
A model-emitted `<tool_result>` receives `CONTROLLER_EVIDENCE_ECHO`.

The controller does not auto-convert `command_groups` to `code`: doing so would
discard ordering, environment, runner, confirmation, and failure semantics.

## 5. Controller transition

```text
successful skill-load observation
  -> canonical loaded skill ID
  -> declared runtime healthcheck
  -> structured runtime evidence
  -> seed/upgrade consultation requirements
  -> ready_to_finalize
  -> FINALIZE compiler
  -> Planner
```

On failure:

```text
invalid action or runtime evidence failure
  -> bounded typed repair
  -> blocked_internal_contract_error when exhausted
  -> finish with audit evidence
```

There is no internal-failure edge to `ask_user`.

## 6. Implementation map

- `bio_agents.py`
  - typed Consultant action errors;
  - controller-confirmed loaded-skill extraction;
  - controller-owned runtime evidence seeding;
  - FINALIZE-before-loop-guard ordering;
  - internal fail-closed loop routing.
- `prompts/templates.py`
  - exact Consultant execute JSON contract and unsupported-field list.
- `agent_core/consultation_state.py`
  - unresolved runtime requirement upgrade from fresh structured evidence.
- `agent_core/consultant_finalize.py`
  - thaw immutable controller constraints before strict validation.

## 7. Acceptance criteria

- The incident `command_groups` payload yields no executable command and a
  typed field-specific repair.
- Fake `<tool_result>` text yields no execution and is classified as controller
  evidence echo.
- An AI load request alone does not prove a loaded skill; a successful
  controller load observation does.
- A successful declared runtime preflight satisfies the canonical skill's
  runtime requirement and allows FINALIZE entry without another Consultant
  discovery call.
- Repeated internal failures terminate internally with no pending human
  question.
- Deterministic and model-batched FINALIZE paths compile, replay from the
  protected checkpoint, and route to Planner.
- The declared snpEff healthcheck resolves an absolute environment prefix, jar
  path, and Java home under the BioMind-loaded `.env` configuration.
- Python compilation, whitespace validation, and the relevant Consultant and
  FINALIZE regression suites pass.

## 8. Environment boundary

BioMind loads `.env`, which defines `BIO_SOURCE_INSTALL_DIR` and lets the
snpEff healthcheck select the independent Temurin 21 runtime. A raw shell that
does not load this environment can fall back to the conda Java and fail with a
GraalVM `PolyglotException` class error. That shell-only failure is not evidence
that the BioMind controller healthcheck fails; production verification must use
the controller environment contract.
