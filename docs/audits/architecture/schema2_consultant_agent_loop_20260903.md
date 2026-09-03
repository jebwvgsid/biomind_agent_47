# Schema-2 Consultant Agent Loop Audit

Date: 2026-09-03
Base HEAD: `65cfcfd739335c39839eec5d327d61691b713b52`

## Pre-change live chain

The production chain was:

`Consultant prompt -> model turn -> legacy command normalization -> Skill/resource action -> ConsultationState mutation -> evaluate_consultation_completion -> TypedConsultant -> ConsultantOutcome -> runtime_graph edge`.

The concrete implementations were located in:

- `PromptManager.get_consultant_v2_prompt`
- the `consultant` closure in `BioAnalystAgent._build_graph`
- `seed_structured_evidence`, `evaluate_consultation_completion`, and
  `evaluate_consultant_candidate_route`
- `BioAnalystAgent._commit_consultant_update` and
  `commit_consultant_candidate_state`
- `TypedConsultant.classify`
- `ConsultantOutcome` and `build_schema2_production_graph`
- `_terminal_status_from_snapshot`, `_interactive_run_result`, and
  `OutputRouter.render_terminal_result`

### Audit answers before the change

1. Fact observations: Skill search candidates, loaded Skill documents, input
   audit rows, runtime evidence, execution observations, and file metadata.
2. Business decisions: `evaluate_consultation_completion`,
   `evaluate_consultant_candidate_route`, runtime preflight, and the
   `TypedConsultant` phase whitelist could decide continue, Planner, or terminal.
3. Implicit Controller authority: the candidate completion gate, runtime route
   gate, automatic ASK resolution to Planner, and adapter phase whitelist.
4. Real protocol invariants: action/JSON shape, TaskSpec mutation shape,
   revision, CAS, accepted-handoff commit, typed-outcome validation, and internal
   evidence reference integrity.
5. Pure consultation without TaskSpec: no formal schema-2 completion path.
6. Formal direct consultation completion outcome: absent from
   `ConsultantOutcome`.

## Implemented ownership

The new path is:

`Reason -> one local action -> observation/mutation receipt -> Reason -> ANSWER_USER or SUBMIT_PLANNING_HANDOFF`.

`classify_consultation_lifecycle` is the shared route-free domain classifier.
It emits only `continue`, `needs_user`, `answer_ready`, `planning_ready`,
`blocked`, or `invalid`. It does not inspect file existence, selected Skill
count, TaskSpec contents, or scientific requirements. `TypedConsultant` is a
thin mapping adapter. Only the graph maps `ConsultantOutcome` to a node.

Resource claims use the minimum epistemic distinction `conceptual`,
`user_asserted`, and `observed`. `inspect_resource` and `glob_resources` return
bounded facts only. A missing path has no disposition or routing field.

`ANSWER_USER` commits an answer receipt and yields `answer_ready`; no TaskSpec is
required. `UPDATE_TASKSPEC` and `SELECT_SKILL` return observations to another
Consultant turn. `SUBMIT_PLANNING_HANDOFF` is the only new schema-2 action that
can produce planning readiness, and only after structural validation plus the
existing revision/CAS/accepted-handoff commit.

Schema-2 readiness additionally requires the durable accepted handoff to carry
`handoff_kind=execution_planning`, `agent_initiated=true`, and a positive
`handoff_protocol_version`. The older versioned `finalize_protocol=compiled`
receipt remains accepted only by compatibility callers; it cannot make the
schema-2 Consultant or Planner adapter ready.

Schema-2 resume now feeds durable ConsultationState through the same shared
lifecycle classifier. It no longer has a local phase whitelist, so a durable
`needs_evidence` state (including one that already has a partial TaskSpec)
resumes as `consultation_ready`.

Legacy `FINISH`, `evaluate_consultant_candidate_route`, and runtime completion
gates remain for schema-1 compatibility. A schema-2 Consultant receives repair
evidence if it emits legacy `FINISH`; that path cannot invoke the legacy gate.

## Schema-2 graph

The complete graph remains:

`START -> Triage -> Consultant -> Planner -> PlanReview -> Generate/StepWorker -> Execute -> Supervisor -> CommitStep -> FinalAudit`, with typed AskUser, InstallSubagent, Recovery, stale-result, and terminal edges.

The Consultant edges are now:

- `consultation_continues -> Consultant`
- `user_input_needed -> AskUser`
- `answer_ready -> END` (the answer is already present in the message state)
- `planning_ready -> Planner`
- `stale_result -> Consultant`
- `terminal/harness_error -> END`

## No-progress and diagnostics

Repeated node names are not treated as a loop. Progress is measured from the
semantic action plus its observation. A changed Skill result, resource fact,
TaskSpec revision, question, or handoff receipt resets identical-no-progress.
Repeated identical action and observation reaches
`CONSULTANT_AGENT_NO_PROGRESS`, an internal diagnostic rather than a business
block.

Front-half harness outcomes project `source_node`, `error_code`, `detail`, and a
compact semantic state into `terminal_diagnostic`. These fields have no routing
authority.

## OTHER_AGENT_LOOP_AUDIT

- Planner: candidate for a future local repair loop. The current adapter and
  source still use validation-issue ownership to choose reconsultation,
  self-repair, or terminal behavior.
- Generate: already resembles propose/validate/repair, but its adapter still
  infers dispositions from legacy exit-reason families. It is a medium-priority
  candidate for a first-class action/observation loop.
- Recovery: highest-priority follow-up. When an explicit disposition is absent,
  `TypedRecovery` derives repair, replan, or revalidation from failure owner and
  source type.
- Execute: keep as a deterministic side-effect boundary. OS failures are real
  execution observations and should not be converted into consultation gates.
- Supervisor: keep deterministic validation and CompletionGate checks. If
  open-ended recovery reasoning is needed, it belongs in Recovery rather than
  the validator.
- PlanReview: approval/CAS checks are protocol. Semantic revision reasoning may
  later use a local agent loop, but no change was justified in this task.

No Planner agent source, Generate, Execute, Recovery, Supervisor, or Pi
implementation was changed by this migration. The typed Planner entry adapter
was tightened only at the shared explicit-handoff protocol boundary so it
cannot accept a legacy compiled receipt as schema-2 authority.
