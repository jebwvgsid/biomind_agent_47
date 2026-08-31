# User Information Flow Audit

Date: 2026-08-02

## Current flow

```text
Initial HumanMessage
  → create_task_spec / extract_request_contracts
  → Triage
  → Consultant prompt
  → requirements_summary + selected skills + runtime audit
  → Planner assembled prompt
  → PlanTracker/current step
  → Generate current-step context
  → Execute evidence
  → Supervisor / CompletionGate / RecoveryContext

Later user answer or correction
  → <human_answer> HumanMessage
  → apply_user_feedback
  → TaskSpec.user_directives + confirmed_decisions
  → bounded render/projection into later nodes
```

The initial extractor is `extract_request_contracts()` (`agent_core/task_spec.py:267-339`) and is called while creating the initial TaskSpec (`349-398`). Later answers use `apply_user_feedback()` (`878-924`), which records directive and decision text but does not call the extractor again.

## Structured versus textual storage

| Fact class | Initial request | Later correction |
| --- | --- | --- |
| File path/role | Can become `InputBinding` | Usually directive text only |
| Output directory/output file | Can become output binding/requirement | Usually directive text only |
| `gtf22`, custom genome, prohibition | Can become requirement/constraint | Usually directive text only |
| Species/reference version | Mostly textual unless separately inferred | Directive text only |
| User answer | Initial request/goal | `user_directives` and `confirmed_decisions` |

TaskSpec schema source priority (`agent_core/task_spec.py:37-48`) protects `user_explicit` structured values from weaker Consultant/Planner-derived values. There is no equivalent global arbitration for duplicate natural-language sections in prompts. Multiple explicit statements can coexist, and latest-wins behavior depends on each projection retaining and interpreting revision order.

## Seven-turn result

Input conversation:

1. custom plant VCF annotation;
2. species is `Arachis hypogaea`;
3. VCF path;
4. FASTA path;
5. GTF path and `-gtf22`;
6. do not guess an existing species database;
7. replace output directory.

After applying turns 2-7, all text existed in `user_directives`/`confirmed_decisions`, but `inputs` remained empty and only the initial `custom_genome` requirement was structured. The resulting visibility depended on projection:

| Stage | What preserved the facts | Loss observed |
| --- | --- | --- |
| Consultant | Reduced TaskSpec + formal input audit + latest answer | Species dropped by 2,000-char TaskSpec limit |
| Planner initial | 3,500-char TaskSpec + Consultant summary | None in the probe |
| Planner repair | Invalid-plan/validation repair prompt | All tested user facts absent |
| Generate | TaskSpec constraint/directive projection + current step | None in the probe |
| RecoveryJudge | Current step/action/evidence task summary | Species, `gtf22`, prohibition absent |
| Supervisor | Step/action/evidence, not dialogue | User facts unavailable unless in contract |
| CompletionGate | Structured TaskSpec/contract | Text-only later facts are fragile |

## Where facts can be lost

### 1. Later feedback is not restructured

`apply_user_feedback()` appends text and a confirmed decision. A later corrected path or output directory is therefore not guaranteed to replace the structured initial binding. This can lead to an old explicit binding and a newer textual correction coexisting.

### 2. Per-node clipping differs

Consultant renders TaskSpec at 2,000 characters (`bio_agents.py:9374`), Planner at 3,500 (`10047`), and Generate uses a separate projection (`2174-2198`). A fact can be visible in Planner and invisible in Consultant or Recovery without state loss.

### 3. Planner repair is not the same prompt as Planner initial

The repair call (`bio_agents.py:10365-10403`) does not automatically carry the effective TaskSpec projection used by the initial Planner call. In the prompt capture it contained none of the seven facts.

### 4. Recovery task summary is incomplete

`build_recovery_context()` projects goal, selected task fields, current step, evidence and attempts, but not the complete later directives, requirements, expected outputs and confirmed decisions. Recovery therefore relies on facts copied into the current step/action.

### 5. Compression summary is not the managed-task source of truth

Compression summaries remain in main messages, but bounded managed-node selectors usually omit generic SystemMessages. SessionArchive retrieval is separate and can be cross-task within the same project. Neither should be relied on for exact current paths/parameters.

## Effective priority today

The intended ordering is only partially implemented:

```text
structured user_explicit binding
> weaker structured inferred/derived binding
```

Across textual prompt sections, the practical ordering varies by node. There is no single effective-facts resolver guaranteeing:

```text
latest explicit user correction
> earlier explicit request
> confirmed Consultant answer
> Consultant inference
> archive summary
> skill default
> Planner/Generate inference
```

This is the central information-flow defect. The recommended fix is not to pass full chat everywhere; it is to make the existing TaskSpec the exact, revision-aware source of current user facts and project the same bounded view into each node.

## Consultant output destinations

Consultant currently persists:

- `requirements_summary`: natural language;
- `selected_skills`, `selected_skills_raw`, selection source;
- `runtime_audit`;
- workflow state when applicable;
- memory/wiki packet context;
- action and summary messages;
- TaskSpec updates from user feedback.

The TaskSpec schema supports structured inputs, requirements, expected outputs, parameters, constraints, decisions, and directives. The main gap is not absence of schema; it is that later multi-turn text is not consistently extracted/merged into those structures.

