# Installer Versus Main Recovery

Audit date: 2026-07-31

The Installer does not use the main Agent recovery protocol. It is a separate
LangGraph with its own prompt, observations, RAG and loop guards.

## Protocol Comparison

| Capability | Main analysis Agent | Installer |
|---|---|---|
| Failure record | Structured `ExecutionRecord` plus full stream/script paths | Short textual observation |
| Task contract | TaskSpec + current Plan step + success criteria | Installer work order |
| Validation | Supervisor `ValidationEvidence` + CompletionGate | Registration/runtime-contract checks inside Installer |
| Diagnosis | Deterministic diagnosis; optional RecoveryJudge | Installer LLM reasons directly from local messages |
| Recovery decision | Typed `RecoveryPolicy` / `RecoveryDecision` | Installer LLM chooses the next command |
| Repair generation | Main Generate under current-step action contract | Same Installer LLM emits Markdown command |
| RAG | On-demand memory/RAG blocks and skill policy | Dedicated install query, then error-KB query using failed observation |
| Logs | Full stdout/stderr persisted; bounded projections | About 500 chars each in observation |
| Retry budgets | Typed PlanTracker budgets by action category | Max 10 Generate turns; repeated observation guard at 3 |
| Final success | Step/task CompletionGate | Successful registration/runtime contract |
| Routing | Controller consumes committed decision | Installer graph loops Generate -> Execute internally |

## Installer Message Composition

Implementation: `agent_core/installer.py:119-183,414-507`.

First turn:

1. Installer SystemMessage;
2. HumanMessage containing the installation work order;
3. optional RAG SystemMessage with installation guidance.

Later turns select at most six messages / 12k characters:

- first Installer system message;
- first work-order human message;
- latest failed observation;
- latest AI installer response;
- latest system/RAG guidance.

On failure, Installer queries its errors knowledge base using a short tail of
the observation. It then directly asks its LLM for another installation command.
It does not construct the main `DiagnosticContext`, invoke RecoveryJudge or
apply RecoveryPolicy.

## Error Detection And Registration

Installer recognizes:

- command return code and textual observation;
- repeated identical observations;
- maximum Generate-turn exhaustion;
- registration tags emitted by the model;
- runtime contract / healthcheck success before final registration.

The independent guard prevents infinite generation, but it cannot reason from
the main Agent's complete execution evidence. This explains historical cases
where an environment was usable but Installer repeatedly proposed reinstalling
it: the work order and short observation dominated, while previous structured
runtime evidence was not part of a shared recovery context.

## Main-Agent Handoff

The main `install_subagent` node assembles selected skill/runtime requirements
and launches the Installer around `bio_agents.py:13952-14330`. It also limits
repeated installation attempts for an environment. However, once inside the
Installer graph, recovery is governed by Installer-local state.

The Installer's direct `llm.ainvoke()` at `agent_core/installer.py:507` also
bypasses the central main-Agent LLM timeout and prompt-character instrumentation.

## Recommended Boundary

Do not merge Installer into Supervisor or CompletionGate. Instead, reuse a
small common evidence contract:

- expected environment and tools;
- actual prefix/executables/versions;
- exact healthcheck and result;
- failed command, return code and full-log paths;
- install attempt fingerprint;
- registration status;
- explicit `install`, `repair`, `reuse`, `block` outcome.

Installer may keep its specialized command generator and install RAG, but its
result should return this structured evidence to the main Controller. The main
Agent should never infer installation success from a prose message alone.

## Conclusion

Installer and the main Agent use different correction contexts and different
decision protocols. The main path is evidence -> policy -> constrained Generate;
Installer is observation/RAG -> Installer LLM -> command. The divergence is
intentional at the workflow level but currently too large at the evidence and
budget level.

