# Current Recovery Context Baseline

Current implementation baseline HEAD:
`8ae7db95ff28b4f85f8f19d2c6c251046d062251`

Baseline date:
2026-09-03 (Asia/Shanghai)

Canonical full baseline audit (historical snapshot, audited HEAD
`9549734ba72ce783cddf579144a699fb67ec7d5e`):
[`recovery_context_engineering_audit_20260903.md`](recovery_context_engineering_audit_20260903.md)

Latest incremental implementation record:
[`recovery_context_engineering_delta_20260903.md`](recovery_context_engineering_delta_20260903.md)

Component matrix:
[`recovery_context_component_matrix_20260903.tsv`](recovery_context_component_matrix_20260903.tsv)

Current relevant source blobs:

| Path | Blob |
|---|---|
| `agent_core/node_context.py` | `8cfcca6dec5a0cbb001019d3c7ce52aa9ca90677` |
| `agent_core/middleware.py` | `f4256c3a3820eaf248c8542457cb82f2483d86df` |
| `agent_core/recovery_evidence.py` | `753b8b700e46dd1ab8db0ee4e3c8d4014fcdfdf7` |
| `agent_core/provider_message_safety.py` | `836a79ef88085aece3390b6af29f211dc303998f` |
| `agent_core/recovery_provider.py` | `6d05d0ab7578f0e929889636f8ca6a069834ea70` |
| `bio_agents.py` | `5990276b4a200e7f5251edba844f470078f90259` |
| `prompts/manager.py` | `7ff26534c53244bad6d5062975ac8ba08835698c` |

The full audit remains the architectural inventory. The delta is authoritative
for the P0 history, complete-message guard, evidence continuation, invocation,
and provider-capture rows that it changes.

Re-audit required when:

- Recovery prompt builder or Recovery message ordering changes
- Recovery semantic view/projection changes
- NodeContext Recovery projection, stripping, payload validation or complete-message safety changes
- selected Skill / Skill access / TaskSpec / Consultant handoff injection changes
- approved Plan/current-step/dependency projection changes
- Execute evidence, artifact or log-bounding schema changes
- state/recovery lifecycle or history-scoping schema changes
- provider invocation/message assembly changes

后续遇到“Recovery 看到了什么”的问题，应首先查阅本文件及其指向的当前
baseline；仅当代码 HEAD 涉及上述边界变更时才重新审计。
