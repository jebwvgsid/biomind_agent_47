# SCHEMA2-ADAPTER-01 Semantic Coverage Audit

Date: 2026-09-03
Base HEAD: `0eb8f0c4653620dc5fd4ffcc76800baee11b6f24`

## Result

The real canary `session_1788441953` exposed a general adapter defect: a
successful legacy business transaction could commit authoritative semantic
state while the schema-2 adapter ignored that state and fell through to a
generic continuation. The schema-2 graph then treated classifier uncertainty
as permission to self-loop.

The fix makes completion/continuation decisions from durable business facts,
removes legacy recovery projections from Generate/Execute classification, and
fails closed when neither completion nor continuation is proven.

Post-fix canary `session_1788444307` exposed the complementary fresh-session
timing case. `SEARCH_SKILLS` produced a valid active `consultation_state` in the
current LangGraph node result, while no canonical TaskSpec was supposed to be
durable yet. The adapter reloaded `plan_state.json` and ignored that node-local
semantic result, so it failed closed with
`CONSULTANT_SEMANTIC_STATE_AMBIGUOUS/canonical_task_spec_missing`. Intermediate
Consultant lifecycle classification now uses the route-blind current node
result when present; the final `planning_ready` decision remains gated solely
by the durable committed predicate below.

## Consultant predicate

Before:

```text
FINALIZE schema 4 compiled
OR (TaskSpec initialized AND pending_action present)
```

After:

```text
canonical TaskSpec initialized at a positive revision
AND no unresolved durable question
AND (
  versioned FINALIZE receipt is compiled
  OR (
    accepted Consultant handoff exists
    AND consultation phase/status/decision is planning-ready
    AND all mandatory consultation requirements are satisfied
    AND handoff Skill selection agrees with durable Skill selection
  )
)
```

No destination or Controller routing field participates.

## Adapter coverage matrix

The `CURRENT` column describes the classifier after this task.

| Adapter | Legacy business state | Authoritative durable facts | Current classifier condition | Expected typed outcome | Current typed outcome | Covered | Ambiguous | Fix required |
|---|---|---|---|---|---|---|---|---|
| Triage | chat request | normalized `task_mode=chat` and request intent | exact supported mode | `direct_response_ready` | same | YES | NO | YES: unknown modes now fail closed |
| Triage | managed request | normalized `task_mode=managed` | exact supported mode | `consultation_ready` | same | YES | NO | NO |
| Triage | readonly/simple request | normalized mode | exact supported mode | `step_execution_ready` | same | YES | NO | NO |
| Consultant | select-skill canonical commit | initialized TaskSpec, positive revision, accepted handoff, ready lifecycle, satisfied mandatory requirements | `consultant_planning_readiness.ready` | `planning_ready` | same | YES | NO | YES |
| Consultant | versioned FINALIZE success | initialized TaskSpec plus compiled schema-4 receipt | `consultant_planning_readiness.ready` | `planning_ready` | same | YES | NO | PRESERVED |
| Consultant | bounded discovery continues | current node-local or durable active consultation phase and non-ready completion decision | explicit lifecycle continuation | `consultation_continues` | same | YES | NO | YES: fresh-session timing covered; no generic fallthrough |
| Consultant | unresolved question | durable pending question | question receipt exists | `user_input_needed` | same | YES | NO | YES: durable fallback added |
| Consultant | stale commit | stale-result receipt | stale receipt exists | `stale_result` | same | YES | NO | NO |
| Consultant | terminal/protocol block | durable terminal task status or blocked contract | explicit terminal fact | `terminal` | same | YES | NO | NO |
| Consultant | unknown ready-like state | no accepted/compiled handoff and no active lifecycle | neither completion nor continuation proven | `harness_error` | same | YES | YES, closed | YES |
| Planner | committed current Plan | materialized current PlanSpec/PlanSnapshot and no blocking validation issues | `has_materialized_plan` | `plan_review_ready` | same | YES | NO | YES: shell revision no longer counts |
| Planner | semantic contract incomplete | Consultant readiness false and no materialized Plan | readiness reason | `consultation_required` | same | YES | NO | YES |
| Planner | candidate correction active | active planning failure or active epoch with positive call count | explicit monotonic planning receipt | `planning_continues` | same | YES | NO | YES |
| Planner | stale CAS result | changed-precondition receipt | stale receipt exists | `stale_result` | same | YES | NO | NO |
| Planner | no Plan and no progress receipt | neither materialized Plan nor active epoch/failure | no valid exit proven | `harness_error` | same | YES | YES, closed | YES |
| PlanReview | approval interrupt pending | durable waiting-state stage | waiting receipt exists | `approval_pending` | same | YES | NO | NO |
| PlanReview | approved current Plan | materialized current Plan plus current approved/accepted status | both facts agree | `approved_for_execution` | same | YES | NO | YES: materialized Plan required |
| PlanReview | semantic feedback | pending review classification or task/input-owned issue | explicit owner/classification | `reconsultation_required` | same | YES | NO | NO |
| PlanReview | plan correction | materialized unapproved Plan and plan-owned validation state | approval absent/current issues | `revision_required` | same | YES | NO | NO |
| PlanReview | approval without a real Plan | approved label but no PlanSpec/PlanSnapshot | no materialized Plan | `harness_error` | same | YES | YES, closed | YES |
| AskUser | answer accepted after interrupt | matching question id, `answered_pending_interpretation`, non-empty answer-turn ref | matching receipt | `answer_recorded` | same | YES | NO | YES |
| AskUser | no question on entry | no pending question | entry fact | `question_missing` | same | YES | NO | NO |
| AskUser | source returns without answer receipt | pending question but no matching answer turn | receipt absent | `harness_error` | same | YES | YES, closed | YES |
| InstallSubagent | runtime usable | at least one current runtime healthcheck record has `ok=true` or current install step succeeded | healthcheck/step receipt | `capability_available` | same | YES | NO | YES: text parsing removed |
| InstallSubagent | install failed | failed healthcheck or blocked/failed current install step | healthcheck/step receipt | `installation_failed` | same | YES | NO | YES |
| InstallSubagent | message says success only | no runtime/step receipt | no authoritative evidence | `harness_error` | same | YES | YES, closed | YES |
| Generate | validated/adopted action | structured `pending_action` | action fact | `action_ready` | same | YES | NO | NO |
| Generate | current completion claim | structured completion candidate | claim fact | `completion_claimed` | same | YES | NO | NO |
| Generate | user/plan/skill/provider/stale/terminal/cancelled exit | corresponding typed source receipt and exit reason | explicit fact family | corresponding typed outcome | same | YES | NO | YES: cancellation added |
| Generate | repairable candidate rejection | repairable exit reason plus `action_format_repair` | changed candidate/error fingerprint and budget | `candidate_rejected` | same | YES | NO | YES: legacy recovery fallback removed |
| Generate | unknown legacy continuation | no supported semantic fact | no valid exit proven | `internal_protocol_error` | same | YES | YES, closed | ALREADY CLOSED |
| Execute | launched command succeeds/fails | scoped ExecutionRecord/attempt and return code | execution evidence | `execution_succeeded` / `execution_failed` | same | YES | NO | NO |
| Execute | diagnostic/lightweight completion | command role or lightweight task mode plus ExecutionRecord | execution evidence | corresponding completion outcome | same | YES | NO | NO |
| Execute | approval/input/install/cancel/stale/block | corresponding structured receipt | explicit fact family | corresponding typed outcome | same | YES | NO | NO |
| Execute | prelaunch semantic rejection | explicit rejection/failure/contract receipt | structured rejection facts | `pre_execution_rejected` | same | YES | NO | YES: route projections removed |
| Execute | only legacy recovery/route fields | no execution or rejection receipt | no valid exit proven | `harness_error` | same | YES | YES, closed | YES |
| Supervisor | valid/warning/invalid/incomplete/diagnostic evidence | ValidationEvidence, CompletionGate and scoped execution facts | deterministic verifier classification | corresponding `VerificationOutcome` | same | YES | NO | NO |
| CommitStep | verified step commit | current VerificationOutcome, PlanState generation and scoped attempt | CAS transaction and durable receipt | `next_step_ready` / `plan_complete` | same | YES | NO | NO |
| CommitStep | conflict/rejection/harness | generation or contract invariant evidence | explicit invariant failure | corresponding failure outcome | same | YES | NO | NO |
| Recovery | repair/replan/reconsult/revalidate/input/block | typed upstream source outcome, scoped incident identity, durable budget/receipt | deterministic disposition | corresponding `RecoveryOutcome` | same | YES | NO | NO |
| Recovery | invalid/colliding disposition | missing/conflicting source identity | explicit identity invariant | `internal_protocol_error` | same | YES | NO | NO |
| FinalAudit | complete task | successful execution, valid verification, durable plan-complete receipt, all steps terminal, CompletionGate pass | scoped terminal CAS | `task_verified` | same | YES | NO | NO |
| FinalAudit | stale/incomplete/error | missing/stale typed evidence or failed gate/auditor | explicit evidence invariant | `entry_rejected` / `task_incomplete` / `harness_error` | same | YES | NO | NO |

## ResumeState coverage

Realistic durable fixtures now cover exactly one classification for all eleven
supported values:

`awaiting_user_input`, `consultation_ready`, `planning_ready`,
`plan_review_pending`, `step_execution_ready`, `action_execution_ready`,
`verification_pending`, `recovery_pending`, `installation_pending`,
`final_audit_pending`, and `terminal`.

An initialized TaskSpec in a ready-like lifecycle without a committed handoff
or compiled receipt now raises `SCHEMA2_RESUME_SEMANTIC_STATE_AMBIGUOUS`; Resume
does not guess a graph destination.

## Fallthrough and loop safety

- Consultant self-entry requires an active route-blind consultation lifecycle
  from the current node result or durable state and a non-ready completion
  decision. A transient ready-like lifecycle cannot produce `planning_ready`;
  the canonical TaskSpec and handoff must already be durably committed.
- Planner self-entry requires an active durable planning failure or planning
  epoch with a positive call count.
- Generate corrective self-entry requires a changed candidate/error
  fingerprint and a bounded budget receipt.
- Recovery decisions remain scoped, identity-stable and budgeted; Recovery has
  no generic self edge.
- Unknown Consultant, Planner, AskUser, InstallSubagent and Execute states now
  fail closed instead of being interpreted as continuation/rejection.

## Route poisoning

Paired fixtures add/remove/corrupt `next_step`, `next_route`, `target_route`,
`proposed_route`, `effective_route`, `dispatch_route`, `route_resolution`,
`required_next_action`, `pending_recovery_action`, and `resume_route_hint`.
Outcomes are unchanged. Static source assertions also prove that classifier
methods and the schema-2 Resume classifier do not read those fields.

## Verification

- `154` focused CTRL-02/CTRL-03, typed adapter, StepWorker, GraphInterrupt,
  CommitStep, Recovery, CompletionGate, PlanReview and Controller-authority
  tests passed.
- `15` semantic coverage tests now pass: the original `13` included in the
  154-test audit plus `2` post-canary timing/commit regressions.
- The post-fix fresh-session regression uses the live consultation/discovery
  state builders and proves `SEARCH_SKILLS -> consultation_continues` before a
  durable handoff, with all legacy route fields poisoned and scrubbed.
- A paired negative regression proves transient `ready_to_plan` cannot bypass
  durable TaskSpec/handoff commit. The focused post-fix suite passes `128`
  tests plus `152` subtests; the select/load/re-entry Consultant integration
  case also passes.
- The preserved real canary state was read without modification and currently
  classifies as `ready=true`, reason
  `accepted_consultant_handoff_ready`, TaskSpec revision `1`.
- No provider/API call and no scientific workflow was run.

The older `test_consultant_first_graph_integration_v21` suite was also invoked
as an audit. Eight cases fail before schema-2 adapter classification because
their Planner fixtures are rejected by the current Plan contract with
`ACTION_CAPABILITY_SCOPE_MISSING`; the failures do not exercise the changed
adapter code and are not treated as evidence for or against this fix.
