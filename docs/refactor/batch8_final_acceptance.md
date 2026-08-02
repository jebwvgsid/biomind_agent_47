# Batch 8 Final Acceptance

## Scope

Batch 8 was implemented as five independently committed sub-batches:

| Batch | Commit | Result |
| --- | --- | --- |
| 8A | `7f76411` | Generic multi-turn facts and revision-aware TaskSpec merge |
| 8B | `e747484` | Deterministic effective-facts resolver and audience projection |
| 8C | `21243aa` | Shared effective-facts projection across managed nodes |
| 8D | `e8a2e60` | Exact LLM request snapshots and decision lineage |
| 8E | pending at report generation | Replay, migration, compression, and observability acceptance |

The implementation reuses TaskSpec, PlanTracker, execution provenance, recovery records,
MemoryManager, and the existing LLM invocation wrappers. It does not implement Batch 9
Recovery Level 2 reasoning or investigation tools.

## Acceptance Results

1. **Are subsequent user facts structured?** Yes, for facts that can be extracted or
   normalized generically. Artifact, entity, parameter, constraint, decision, and relation
   bindings are persisted with revision and source metadata. Information that cannot be
   mapped safely is retained as provisional or unresolved instead of being discarded.

2. **Is there only one EffectiveUserFacts resolver?** Yes.
   `resolve_effective_user_facts()` is the sole deterministic active-value resolver;
   projections do not re-resolve precedence.

3. **Do Consultant, Planner, Repair, and Generate use the same active facts?** Yes. They
   receive audience- and step-scoped projections from the same resolved revision. The
   focused tests also cover routing, Recovery Level 1, Supervisor, and CompletionGate
   projections.

4. **Is the final prompt for each key LLM call saved?** Yes for new calls of kinds
   `task_router`, `consultant`, `planner_initial`, `planner_repair`, `generate`,
   `recovery_judge`, `supervisor_reviewer`, and `installer`.

5. **Are snapshots captured at call time?** Yes. The final ordered message payload is
   persisted immediately before model invocation. It is not reconstructed from later state.

6. **Are Consultant candidate and selected skills saved?** Yes. Candidate IDs, selected
   IDs, skill versions, ConsultHandoff, and runtime audit annotations are attached to the
   call snapshot.

7. **Are Planner initial and repair calls both saved?** Yes. They have distinct call kinds
   and immutable request/response snapshots.

8. **Are Generate input context and generated scripts saved?** Yes. The exact Generate
   request/response is preserved, while generated actions/scripts are linked through
   PlanTracker and execution provenance.

9. **Are generated and executed scripts both saved?** Yes. Execution lineage records both
   references and hashes, with a diff when their contents differ.

10. **Do Renderer-backed steps have a complete trace?** Yes. A Renderer route does not
    require a synthetic Generate call. Controller route events, Renderer input/manifest,
    manifest actions, attempts, executed scripts, and validation evidence are linked.

11. **Can DecisionLineageBundle replay the full decision chain?** Yes. It deterministically
    assembles user/task state, routing, Consultant, Planner, Generate or Renderer, execution,
    and validation evidence without invoking an LLM.

12. **Is unrelated session history excluded?** Yes. The default scope is the current task,
    current plan lineage, and requested step/attempt ancestry.

13. **Can explicitly inherited source text and artifacts be included?** Yes. Explicit
    `source_task_id` references add the source task's user/router/Consultant snapshots,
    completion handoff, and artifact heads. Automatic Task A to Task B inheritance remains
    outside Batch 8.

14. **Are over-budget prompts segmented by complete stage?** The lineage producer records
    sizes and emits a four-stage segment plan (`task/user/consult`, `planner`,
    `generate/renderer`, `execute/validate`) with whole-snapshot references and hashes. It
    does not silently character-truncate individual snapshots. Batch 9 will consume this
    plan for Recovery Level 2.

15. **Are API keys or tokens leaked?** No known credentials are persisted. Deterministic
    redaction covers credential fields, environment assignments, and Authorization headers,
    and saves a redaction manifest. Ordinary paths and analysis parameters remain visible.

16. **Is replay idempotent?** Yes. Stable call IDs and atomic immutable files make identical
    replay a no-op; conflicting content for an existing call ID raises an explicit error.

17. **Are facts and trace equivalent across memory compression?** Yes. The acceptance test
    runs the actual MemoryManager compression path (5,008 to 40 tokens) and verifies the
    effective-facts revision, snapshot references, and request hashes are unchanged.

18. **Are missing legacy snapshots reported honestly?** Yes. Pre-Batch-8D calls without
    exact snapshots are reported as `trace_status=legacy_unavailable`,
    `reconstructed=true`, and `not_exact=true`. Existing artifacts do not promote such a
    trace to exact.

19. **Was a second TaskSpec, PlanTracker, or execution-state system created?** No. The trace
    store is an immutable evidence store and keeps only compact references in graph state.

20. **Were out-of-scope Recovery reasoning, Renderer routing, or memory retrieval changed?**
    No decision semantics were changed. Existing call sites gained snapshot/event hooks;
    Recovery Level 2 reasoning, Renderer route redesign, and archive retrieval remain out of
    scope.

## Tests

Focused Batch 5-8 regression command:

```text
python -m unittest \
  tests.test_batch8_replay tests.test_context_lineage \
  tests.test_execution_provenance tests.test_plan_tracker \
  tests.test_renderer_manifest tests.test_renderer_controller \
  tests.test_recovery_context tests.test_plan_lifecycle \
  tests.test_completion_gate tests.test_supervisor \
  tests.test_effective_context_projection tests.test_batch8_user_facts \
  tests.test_effective_user_facts tests.test_task_spec \
  tests.test_installer_agent tests.test_bio_agents_memory
```

Result: **330 tests passed**.

The replay suite covers seven-round fact revision, GT21 task isolation and explicit
inheritance, injected faults across all decision stages, actual memory compression,
legacy migration, secret-redacted debug dumps, and exclusion of unrelated tasks.

## Operator APIs

- `dump_effective_user_facts(data_path, task_id)`
- `dump_context_lineage(data_path, task_id, step_id, attempt_id)`

Both APIs are read-only and return redacted structures or deterministic JSON. New calls have
exact snapshot references; legacy gaps remain explicitly marked.

## Remaining Boundary

Batch 8 is complete within its declared scope. It does not provide automatic cross-task
inheritance, Recovery Level 2 verdict generation, read-only investigation tools, or archive
retrieval integration. Those remain Batch 9 work.
