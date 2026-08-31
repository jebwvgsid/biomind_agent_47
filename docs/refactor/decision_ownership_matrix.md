# Decision Ownership Matrix

Audit date: 2026-07-30

Target rule: policy components return semantic decisions; the Controller applies
state transitions; PlanTracker is the durable state owner.

| Decision | Current owner(s) | Duplicate/problem | Target single owner | Evidence | Risk |
|---|---|---|---|---|---|
| `step_type` | Planner, plan normalization, occasional Generate/repair assumptions | Renderer composite semantics can be lost or inferred later | Planner contract; PlanTracker persists | `bio_agents.py:9330`, `agent_core/plan_tracker.py:909,1798` | High |
| `skill_id` | Consultant, Planner, active-skill controller, Generate context | selected vs active skills remain separate but several nodes can rewrite projections | Consultant selects; Planner assigns per step; Controller activates | `agent_core/state.py:16-34`, `bio_agents.py:9330` | Medium |
| `renderer_id` | Skill metadata, Planner-derived composite step, Generate action, `_bind_pending_action()` inference | Free-form code can create a Renderer ID by substring matching | Planner step plus SkillManager registry | `bio_agents.py:2527-2560`, `bio_agents.py:2776-2808` | Critical |
| Allowed actions | static Generate prompt, current action contract, format-repair prompt, transition guard | Lists disagree; Renderer steps still see `EXECUTE` | Controller-derived action contract for current step | `prompts/templates.py:198,234`, `bio_agents.py:3267`, `bio_agents.py:10883-10920` | Critical |
| Action format repair | Generate, transition guard, recovery fields | Repair list is generic and can repeat the invalid action class | Existing action parser + Controller protocol repair | `bio_agents.py:10495`, `bio_agents.py:10883-10933` | High |
| Probe permission | Generate prompt, Execute heuristics, RecoveryJudge, knowledge-conflict guard | Probe can be mislabeled as a primary/Renderer action | RecoveryPolicy authorizes; PreAction validation enforces | `bio_agents.py:11244-11363`, `agent_core/recovery.py:1223-1279` | High |
| `command_role` | Generate payload, `_bind_pending_action()`, Execute fallback inference | Textual compatibility inference overrides true probe purpose | Materializer sets; PreAction validation verifies | `bio_agents.py:2516-2599`, `bio_agents.py:11244-11270` | Critical |
| Renderer invocation classification | Generate, parser, `_materialize_renderer_invocation()`, `_bind_pending_action()` compatibility adapter | Two sources of truth | `_materialize_renderer_invocation()` only | `bio_agents.py:2527-2560`, `bio_agents.py:2770-2865` | Critical |
| Manifest gate | Execute controller, PlanTracker, Supervisor artifacts, CompletionGate | Manifest validity and progress are checked in several places | PlanTracker manifest functions; CompletionGate consumes result | `agent_core/plan_tracker.py:496-634`, `supervisor.py:865`, `agent_core/completion_gate.py:1879` | High |
| Action artifact gate | Executor validator, Supervisor, CompletionGate | Latest-action artifacts and step artifacts are mixed | PostExecution validation for action; CompletionGate for step/task | `bio_agents.py:11700`, `supervisor.py:865`, `agent_core/completion_gate.py:1879` | High |
| PASS/WARN/RETRY | Executor validation, Supervisor, RecoveryJudge constraints | Supervisor also routes and increments retry | PostExecution validation emits facts; RecoveryPolicy emits recovery decision | `supervisor.py:1046-1327`, `agent_core/recovery.py:1105` | High |
| `run_probe` | Supervisor retry contract, middleware, post-review recovery, Generate | Several paths can request and route probes | RecoveryPolicy | `supervisor.py:892`, `agent_core/middleware.py:1602`, `bio_agents.py:2344` | Critical |
| Retry budget | Execute graph state, PlanTracker budgets, Supervisor | Counters can diverge or count probes as primary retries | PlanTracker | `bio_agents.py:11292-11320,11726-11734`, `supervisor.py:1049,1280` | Critical |
| Repair budget | Generate durable helper, PlanTracker, recovery paths | Different buckets have local semantics | PlanTracker with typed budget API | `bio_agents.py:3102`, `agent_core/plan_tracker.py:302` | High |
| Blocked | Generate, Execute, Supervisor, recovery middleware, final gate | Internal protocol errors can become terminal at different layers | RecoveryPolicy proposes; Controller commits via PlanTracker | `bio_agents.py:10860-10881,11180-11217`, `supervisor.py:1287`, `agent_core/recovery.py` | Critical |
| Escalation | Supervisor, RecoveryJudge, Controller fallback | Public ESCALATE can mean user decision or internal failure | RecoveryPolicy, with explicit owner/requires-user-input | `supervisor.py:1217-1304`, `agent_core/recovery.py:1388` | High |
| Step completion | Supervisor PASS handling, Generate completion candidate, CompletionGate, PlanTracker | PASS and completion are sometimes conflated | CompletionGate validates; PlanTracker commits | `agent_core/completion_gate.py:1746,1879`, `agent_core/plan_tracker.py:2159` | Critical |
| Task completion | Generate solution candidate, Supervisor final audit, CompletionGate, routers | Natural-language completion and final gate use multiple routes | CompletionGate validates; Controller commits terminal status | `bio_agents.py:10575-10605`, `agent_core/completion_gate.py:2085` | High |
| `next_step` | Generate, Execute, middleware, Supervisor, post-review recovery, routers | This is the broadest duplicated decision surface | Controller transition table only | `agent_core/state.py:8`, `agent_core/middleware.py:1761-1811`, `supervisor.py:1287`, `bio_agents.py:2344,13429-13468` | Critical |

## Target Ownership Clarification

The proposed statement "Planner is the only execution-route decision maker" is
too broad. Planner should own the approved scientific route and step contracts,
but it should not choose runtime graph edges after an execution failure.

The cleaner split is:

- Planner: planned work and contracts.
- Materializer: one action conforming to the current step.
- Deterministic pre-action validation: accept/reject the action.
- Post-execution validation: facts about the latest action only.
- CompletionGate: step/task contract completion only.
- RecoveryPolicy: semantic recovery outcome only.
- Controller: sole writer of graph transition (`next_step`).
- PlanTracker: sole durable owner of status and budgets.
