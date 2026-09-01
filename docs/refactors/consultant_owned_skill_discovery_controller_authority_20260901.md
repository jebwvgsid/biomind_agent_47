# BioMind Consultant-Owned Skill Discovery / Controller Authority Boundary Refactor

日期：2026-09-01

实现状态：COMPLETE — targeted/offline acceptance passed; real model/API and real GWAS were not run

基础提交：`8b7645d00d1f5cb7d42aa404170a0b9d66b662dd`

基础分支：`codex/save-current-work`

实现分支：`refactor/consultant-controller-authority`

## 1. Executive summary

本次修复把四类此前混在 Controller bootstrap 中的语义判断拆回其正确 owner：

- Consultant 形成 search query、请求读取完整 Skill 文档、比较候选、选择/排除 Skill、绑定语义输入，并编译 typed runtime failure policy。
- SkillManager 只提供确定性的 catalog、搜索、文档加载/缓存、canonical ID 校验和 executable-runtime metadata 判断。
- Controller 只执行并校验 typed action、持久化 typed state、核验文件与 runtime machine evidence、做 active-set reconciliation、CAS、路由和 bounded no-progress protection。
- Planner 仍负责把 canonical TaskSpec、effective selected Skills、runtime evidence 与 failure policy 映射为执行 DAG；本次没有让 Controller 构造科学 DAG。

核心行为变化是：raw prompt 不再触发自动搜索、自动全文加载、自动 selection 或 pre-selection runtime requirements；搜索结果只进入 candidate state；selection 必须有当前 digest 的完整文档证据；显式 GFF 不再自动制造 FASTA 需求；runtime unavailable 是已检查的 machine outcome，不再伪装成 Consultant 未完成的 semantic requirement。

## 2. Git base, revised preflight, and branch

按修订后的 Git safety rule，先执行了：

```bash
git rev-parse HEAD
git branch --show-current
git diff --name-only
git diff --cached --name-only
git status --short
git status --porcelain=v1
```

结果：

| Check | Result |
|---|---|
| BASE HEAD | `8b7645d00d1f5cb7d42aa404170a0b9d66b662dd` |
| BASE BRANCH | `codex/save-current-work` |
| tracked modified | 0 |
| staged | 0 |
| merge/rebase/cherry-pick | none |
| untracked outside exact allowlist | 0 |

随后从该 HEAD 创建：

```bash
git switch -c refactor/consultant-controller-authority
```

创建后再次验证 HEAD、branch、tracked/staged diff 与 status；结果仍只有用户给定的八个 allowlisted pre-existing paths。它们在整个实现中未被删除、移动、stash、ignore、修改或暂存。所有提交均使用 `git add <exact path> ...`；未使用 `git add .` 或 `git add -A`。

## 3. Phase 0 — actual discovery implementation at base HEAD

### 3.1 Catalog source and fields

基础实现位于 `agent_core/skill_manager.py::SkillManager._discover_skills()`：启动时对 `skills/**/SKILL.md` 做动态目录扫描，读取 YAML front matter，构建进程内 `discovered_skills` 与 canonical/alias lookup。runtime installation/health 状态另来自 `skills/.skill_runtime_registry.json`，但该 registry 不是科学 selection authority。

实际可索引字段包括：

| Field | Base source |
|---|---|
| `skill_id` | `SKILL.md` 相对目录生成的 `_skill_id` |
| `name` | YAML front matter，必需 |
| `summary` / `description` | YAML top-level 或 nested `metadata` |
| aliases / legacy IDs | `aliases`, `legacy_ids`, leaf directory |
| tags / domain | YAML metadata |
| task/stage types | `task_types`, `stage_tags`, `skill_type` |
| inputs / outputs | YAML metadata |
| runtime metadata | YAML `runtime`, `generate` 加 runtime registry record |
| document path | `_skill_md` / `_skill_dir` |
| document digest | `_skill_content_hash`, SHA-256 of the source `SKILL.md` |

没有 BM25、embedding、vector DB 或外部 search service。Catalog 在进程内缓存；基础版本的 full document loader 每次读取文件，没有摘要感知的 rendered-document cache。

### 3.2 Narrow Phase 0 table

| Component | Current file at base | Current function/path | Base behavior | Target behavior | Implementation decision |
|---|---|---|---|---|---|
| Catalog scan | `agent_core/skill_manager.py` | `_discover_skills()` | 扫描全部 `SKILL.md` front matter，构建内存索引 | 保留完整内部索引，不把完整 catalog 发给模型 | 保留扫描；增加 typed completeness/revision 与 bounded projections |
| Search/ranking | `agent_core/skill_manager.py` | `rank_skill_candidates()` | 对全部 metadata 做确定性 weighted lexical ranking | 只执行 Consultant 提供的 1–3 个 query | 新增 `search_skill_queries()`；stable merge/dedupe/cap |
| Search caller | `bio_agents.py` Consultant bootstrap | `rank_skill_candidates(consultant_context, ...)` | Controller 用完整 raw Consultant/user context 自动发起初始搜索 | Controller 不生成 query | 删除自动调用；只处理 typed `SEARCH_SKILLS` action |
| Raw ID handling | `bio_agents.py` Consultant bootstrap | `explicit_candidate_ids` substring path | raw prompt literal ID 被自动 full-load、selected 并创建 runtime rows | literal ID 仅为 observed evidence | 新增 observed mentions state；不进入 visible catalog/effective selection |
| Full-doc load | `agent_core/skill_manager.py`, `bio_agents.py` | `load_skill()`, `load_skills_for_prompt()` | raw ID 可自动加载；重复读取/重复 prompt injection | 只有显式 Consultant load；digest 未变复用 | bounded batch load + stat/digest-aware cache + incremental injection |
| Discovery state | `agent_core/skill_discovery.py` | `new_skill_discovery_state()` and record helpers | candidate/loaded/selected 部分区分；排除与 digest lineage 不完整 | typed candidate/load/select/exclude refs，sets disjoint | 扩展 schema 与 transition helpers |
| Action protocol | `bio_agents.py`, `prompts/manager.py` | Consultant V2 action parsing | 已有 search/load/select/reject/ask/finish 变体，但 bootstrap 绕过协议 | 复用单一协议，不新增 agent | 保留兼容 uppercase/XML/JSON；canonical JSON action schema typed化 |
| Input resolver | `agent_core/formal_artifact_resolver.py` | `_should_discover_reference_annotation()` | 看到 GFF/FASTA 文本或 suffix 即 companion scan，候选可变成 ambiguity/binding | 只有 typed role request 才找候选；候选不自动 bind | 新增 `candidate_search_roles` / `required_input_roles` |
| Runtime evidence | `bio_agents.py`, `agent_core/consultation_state.py` | runtime seed + `seed_structured_evidence()` | raw mention/candidate 可创建 mandatory unresolved rows；ledger append-like | effective selected executable only；checked 与 availability 分离 | metadata filter + set-diff reconciliation + owner-aware route |
| Finish/no-progress | `agent_core/consultation_state.py`, `agent_core/consultant_binding.py` | completion evaluator and fingerprints | stale machine rows阻止 semantic finish；反馈变化不完全进入 fingerprint | semantic closure independent；真正相同上下文仍 2/2 终止 | active semantic filter + evidence/visible-repair digests |
| Prompt boundary | `bio_agents.py`, `prompts/manager.py` | Consultant V2 NodeContext/capability context | 可自动注入 tree/docs；runtime rows存在重复/遗漏风险 | bounded deltas, one doc copy, visible runtime telemetry | 移除自动 tree/preload；pending-digest injection；runtime rows单路径序列化 |

### 3.3 Existing action names reused

没有创建第二套 agent 或并行 action protocol。保留并规范化的 action 包括：

- `search_skills` / `SEARCH_SKILLS`
- `load_skill_doc` / `LOAD_SKILL_DOC`
- `reject_skill`（typed exclusion evidence）
- `select_skill` / `SELECT_SKILL`
- `ask_user` / `ASK`
- `capability_gap`
- `finish` / `FINISH`

`load_skill_doc` 向后兼容单个 `skill_id`，并支持 `skill_ids: list[string]`（单批 1–5）。`search_skills` 支持 1–3 个 typed query。

## 4. Before-fix complete call chains

### 4.1 Skill discovery/runtime chain

```text
raw user/Consultant context
→ BioAnalystAgent Consultant bootstrap
→ SkillManager.rank_skill_candidates(raw consultant_context)
→ record_skill_search(source=initial_request_retrieval)
→ substring exact IDs from raw context
→ load_skills_for_prompt(exact IDs)
→ record_selected_skills(exact IDs)
→ runtime_seed_skill_ids = raw IDs + loaded IDs + selected IDs
→ runtime audit before Consultant selection
→ seed_structured_evidence(candidate/runtime rows)
→ mandatory unresolved runtime requirements
→ Consultant candidate/finish
→ stale requirements remain at finish gate
→ repeat Consultant finish
→ bounded no-progress terminates at 2/2
```

### 4.2 Input chain

```text
explicit GFF3 path
→ resolve_formal_upstream_artifacts()
→ _should_discover_reference_annotation(raw text / explicit suffix)
→ broad sibling FASTA/GFF scan
→ inferred files promoted to resolved input or authoritative ambiguity
→ formal-inputs unresolved
→ Consultant cannot clear Controller-created ambiguity through TaskSpec alone
```

The 2026-09-01 read-only architecture evidence identified the first incorrect mutation as inferred sibling FASTAs being promoted to `ambiguous_roles.reference_fasta`, followed by the explicit `Real.gff3` being repackaged into annotation ambiguity. It also showed six pre-selection runtime rows, including excluded TASSEL and a routing workflow without an executable runtime.

## 5. After-fix complete call chains

### 5.1 Consultant-owned discovery and selection

```text
raw user goal
→ Controller records literal canonical IDs only as observed_skill_mentions
→ Consultant emits SEARCH_SKILLS with 1–3 semantic hypotheses
→ Controller validates action schema/budget
→ SkillManager.search_skill_queries(Consultant queries)
→ stable weighted-lexical per-query ranking
→ stable canonical-ID merge/dedupe, total <= 12
→ candidate_skill_refs only (no selection/runtime)
→ Consultant emits LOAD_SKILL_DOC for chosen candidates
→ Controller validates canonical IDs and batch/char budgets
→ SkillManager loads/cache-reuses exact requested documents
→ document digest + source + revision + safe summary persisted
→ only new/changed documents injected into the next Consultant payload
→ Consultant emits select_skill with selected/excluded typed decisions
→ Controller validates load-before-select and disjoint decision sets
→ canonical TaskSpec candidate + selected/excluded refs committed
```

### 5.2 Inputs, semantic closure, runtime, and Planner handoff

```text
explicit files
→ resolver verifies exists/file/non-empty/readable
→ observed_artifacts records machine evidence
→ no companion scan unless Consultant/TaskSpec supplies typed candidate_search_roles
→ candidate files remain unbound
→ merge_task_spec_input_audit accepts explicit/user/consultant-confirmed only
→ explicit bindings retain precedence

effective Consultant selection
→ SkillManager.effective_runtime_skill_ids()
→ exclude typed exclusions and routing/non-executable metadata Skills
→ Controller runtime audit selected executable Skills only
→ evidence_status=checked + availability_status=ready/unavailable/broken
→ seed_structured_evidence() replaces active set and retires stale rows
→ semantic completion ignores non-semantic machine availability rows
→ route_runtime_preflight(typed TaskSpec policy, exact machine evidence)
   ├─ ready → Planner
   ├─ block_stage_and_continue_closeout → Planner with RUNTIME_STAGE_BLOCKED
   ├─ allow_substitution=true → Consultant with exact evidence + allowed actions
   └─ fail policy → terminal RUNTIME_UNAVAILABLE, execution_started=false
```

## 6. Authority matrix

| Decision/state | Consultant | SkillManager | Controller | Planner |
|---|---|---|---|---|
| Understand scientific goal/ambiguity | authoritative | no | validates representation only | consumes |
| Form/rewrite Skill queries | authoritative | no | executes typed action | no |
| Rank catalog metadata | requests | deterministic service | validates budget | no |
| Choose documents to read | authoritative | loads requested IDs | validates ID/budget/cache state | no |
| Select/exclude scientific Skills | authoritative | validates metadata existence | validates/persists/disjointness | consumes |
| Bind semantic input roles | authoritative/user | exposes declared input metadata | verifies path evidence and provenance | consumes |
| Decide substitution/failure semantics | authoritative through typed TaskSpec | no | parses typed policy only | maps policy to DAG |
| Check runtime availability | sees evidence | declares metadata/executable nature | authoritative machine check/persistence | consumes |
| Reconcile active evidence set | no | no | authoritative deterministic set diff | no |
| Decide scientific DAG | no after handoff | no | no | authoritative |
| Bounded no-progress / routing | supplies candidate | no | authoritative deterministic guard | downstream target |

## 7. Search query ownership and catalog/search implementation

`bio_agents.py` no longer calls `rank_skill_candidates()` with the full raw request in the ordinary Consultant bootstrap. `SEARCH_SKILLS` now parses typed `queries`, and the exact Consultant-provided query texts are passed to `SkillManager.search_skill_queries()`.

The chosen implementation intentionally remains local and deterministic:

- metadata-only scan over the in-memory catalog;
- normalized lexical tokens, exact ID/alias/runtime-tool matches and weighted metadata fields;
- declared input-role overlap as bounded structured evidence;
- stable sort by descending deterministic score and canonical `skill_id` tie-break;
- maximum 3 queries, maximum 8 results per query, maximum 12 merged results;
- canonical-ID deduplication across hypotheses;
- typed catalog digest/completeness evidence;
- no BM25 dependency, embedding model, vector DB, network or external service.

This is sufficient for the current catalog size and preserves reproducibility. Search scores remain discovery evidence only and cannot promote a candidate to selected.

## 8. Candidate/load/select/exclude state model

The durable discovery state now separates these sets:

| State field | Meaning | Authority/provenance |
|---|---|---|
| `observed_skill_mentions` | exact canonical IDs literally seen in text | non-authoritative evidence only |
| `search_queries_attempted` / `search_history` | exact Consultant queries, result IDs and catalog digest | Consultant action + Controller record |
| `candidate_skill_ids` / `candidate_skill_refs` | bounded metadata search results | deterministic service evidence |
| `loaded_skill_ids` / `loaded_skill_refs` | documents explicitly loaded in this continuation | Consultant request + Controller verification |
| `loaded_skill_docs` | digest, source path, revision, safe summary, cache reference | Controller persistence |
| `pending_document_injection_ids` | newly loaded or changed docs not yet injected | Controller prompt-state bookkeeping |
| `injected_document_digests` | last digest injected per Skill | Controller incremental continuation |
| `selected_skill_refs` | typed role/reason/revision/document digest | Consultant decision |
| `excluded_skill_refs` | typed reason code/reason/revision | Consultant decision |
| `rejected_candidates` | documented incompatibility/gap evidence | Consultant decision |

Selection validates that each selected Skill has a loaded document whose digest matches the current catalog document digest. Selected and excluded sets must be disjoint. A repeated load with unchanged size/mtime/digest reuses the cached rendered document and does not enqueue another prompt injection; a changed source refreshes the digest and is injected once as a new delta.

## 9. Raw prompt shortcut removal

Removed/downgraded behaviors:

- no automatic `rank_skill_candidates(consultant_context)` initial search;
- no substring-derived `explicit_candidate_ids` promoted to selection;
- no `load_skills_for_prompt(explicit_candidate_ids)` bootstrap;
- no raw mention added to `current_selected_skill_ids`;
- no raw mention or unselected candidate added to runtime seed requirements;
- no automatic full skill tree injection during expansion;
- observed literal IDs do not enter the visible catalog unless searched/loaded/selected through the protocol.

Literal canonical IDs remain available in `observed_skill_mentions`, so Consultant can issue a direct exact `LOAD_SKILL_DOC` without losing explicit user evidence. That observation alone has no effect on selection or runtime.

## 10. Input semantic authority

`resolve_formal_upstream_artifacts()` now accepts explicit typed controls:

```text
candidate_search_roles
required_input_roles
```

Reference/annotation companion discovery runs only when one of those typed roles requests it. Results from local companion search use `source=local_candidate_search`, remain in `candidate_groups`, and never become a TaskSpec input automatically.

`merge_task_spec_input_audit()` accepts only evidence with explicit/user/Consultant-confirmed provenance. Local sibling, sidecar and directory candidates are ignored until Consultant confirmation. Consequently:

- an explicit `Real.gff3` without a selected stage requiring FASTA causes no FASTA search or ambiguity;
- when FASTA is actually required, the Controller returns candidates but does not choose one;
- an existing explicit annotation binding cannot be overwritten by multiple similar GFF/GTF candidates;
- file existence/readability remains Controller-owned machine verification, while the semantic role remains user/Consultant-owned.

## 11. Runtime evidence versus semantic closure

Runtime audit scope is now derived from:

```text
effective selected_skill_refs
minus excluded_skill_refs
filtered by SkillManager.is_executable_runtime_skill()
```

It is not derived from raw text, search candidates, all loaded docs or Skill names. Routing/meta Skills can remain selected without receiving a fake runtime requirement.

Each machine row separates:

```text
owner=runtime_preflight
semantic=false
mandatory=false
evidence_status=checked|pending
availability_status=ready|unavailable|broken|unknown|not_applicable
reason_code / reason
active / disposition
```

`seed_structured_evidence()` performs active-set replacement. Unselected/excluded/superseded runtime rows become `active=false`, `mandatory=false`, `status=waived`, `disposition=superseded_or_excluded`. An excluded Skill that still appears in the effective active set creates a deterministic `EXCLUDED_SKILL_STILL_ACTIVE` controller protocol error, not a request for Consultant to repair Controller state.

Semantic finish considers only active mandatory semantic requirements owned by user/Consultant. A checked unavailable runtime is therefore complete evidence with an unavailable outcome; it is not an unresolved semantic fact.

Typed routing outcomes:

| Policy/evidence | Route | Code | Execution started |
|---|---|---|---|
| all effective runtimes ready | Planner | `RUNTIME_READY` | false |
| unavailable + `block_stage_and_continue_closeout` | Planner blocked handoff | `RUNTIME_STAGE_BLOCKED` | false |
| unavailable + `allow_substitution=true` | Consultant | `RUNTIME_SUBSTITUTION_REQUIRED` | false |
| unavailable + fail/default | terminal finish | `RUNTIME_UNAVAILABLE` | false |
| unchecked machine evidence | terminal/internal evidence failure | `RUNTIME_EVIDENCE_NOT_CHECKED` | false |

## 12. Finish and no-progress behavior

A complete `select_skill` response may now atomically provide selection, exclusion, TaskSpec operations and finish semantics. Once the candidate is valid, the route immediately proceeds to runtime preflight/Planner; no ceremonial empty `finish` turn is requested.

The 2/2 bounded no-progress guard remains enabled. It now fingerprints:

- canonical TaskSpec/selection/discovery progress;
- active authoritative evidence;
- the latest model-visible validation feedback/recovery directive;
- selected/excluded/loaded document digests.

A reformulated query, changed catalog result, changed active evidence or changed actionable repair context resets progress. Two genuinely identical invalid responses under the same complete actionable context still terminate at 2/2.

## 13. Prompt payload and incremental continuation boundary

Ordinary Consultant V2 receives one deterministic NodeContext plus a bounded capability context. The payload contains:

- original semantic evidence and current canonical TaskSpec projection;
- latest search query/result deltas and typed candidate cards;
- loaded Skill refs and only newly loaded/changed full documents;
- current selected/excluded decisions;
- latest actionable validation feedback;
- selected-runtime rows once, inside NodeContext feedback;
- telemetry that matches the serialized runtime row count.

It does not contain the full catalog, automatic skill tree, all `SKILL.md` files, repeated full documents, hidden runtime rows, old runtime logs or generic graph history. Full documents are cached by canonical ID and source stat/digest, persisted by digest, and injected only while their ID is pending for the current continuation.

## 14. Files and primary functions changed

| File | Primary changes |
|---|---|
| `agent_core/skill_access.py` | typed bounded search request/service result handling and continuation access state |
| `agent_core/skill_discovery.py` | schema v2-style candidate/load/select/exclude state, batch search/load records, digest validation, progressive discovery/no-progress deltas |
| `agent_core/skill_manager.py` | catalog completeness/revision, deterministic multi-query search, observed-ID evidence, executable-runtime filtering, document cache/digest refresh |
| `prompts/manager.py` | Consultant V2 typed multi-query/batch-load/select/exclude contract and payload boundary instructions |
| `agent_core/formal_artifact_resolver.py` | typed candidate/required-role companion discovery; observed artifacts; no automatic candidate binding |
| `agent_core/task_spec.py` | input-audit provenance filter and typed `runtime_failure_policy()` |
| `agent_core/consultation_state.py` | owner/semantic/active evidence fields, active-set reconciliation, semantic completion filter, runtime route decisions |
| `agent_core/consultant_binding.py` | active-evidence and model-visible-repair digests in no-progress fingerprints |
| `bio_agents.py` | removal of raw-prompt authority, typed action execution, incremental doc injection, effective-runtime audit, owner-aware runtime routing and exact diagnostics |
| `tests/test_formal_artifact_resolver.py` | updated candidate-not-binding expectations for typed companion search |
| `tests/test_controller_runtime_authority.py` | T15–T22 runtime/active-set/no-progress acceptance |
| `tests/test_consultant_authority_boundary.py` | T01–T14 and T23–T24 authority boundary/regression acceptance |
| `tests/test_gwas_offline_authority_replay.py` | complete deterministic GWAS acceptance replay without command/API execution |
| `docs/refactors/consultant_owned_skill_discovery_controller_authority_20260901.md` | this evidence and handoff report |

## 15. T01–T24 targeted acceptance matrix

Command:

```bash
python -m pytest -q \
  tests/test_consultant_authority_boundary.py \
  tests/test_controller_runtime_authority.py
```

Result: `24 passed in 7.47s`; failed `0`; skipped `0`.

| Test | Result | Acceptance evidence |
|---|---|---|
| T01 Catalog Is Internal | PASS | full internal index exists; prompt has only bounded card and no full-doc sentinels |
| T02 Query Comes from Consultant | PASS | exact Consultant query reaches service; raw prompt is not selected/search input |
| T03 Multi-Hypothesis Search | PASS | 3 queries execute; merged IDs stable, deduped and capped |
| T04 Search Result Is Candidate Only | PASS | candidate refs populated; selection/runtime unchanged |
| T05 Raw Canonical ID Mention | PASS | observed only; not selected/runtime-required |
| T06 Direct Load | PASS | document/digest persisted; unchanged digest reuses cache/no reinjection; changed digest queues one delta; no selection |
| T07 Load Before Select | PASS | typed Consultant-owned `SKILL_DOC_REQUIRED`, allowed action `load_skill_doc`; clears after load |
| T08 Selected/Excluded Typed State | PASS | EMMAX selected, TASSEL excluded, disjoint, source Consultant |
| T09 No Extra Empty Turn | PASS | complete selection + TaskSpec routes directly to Planner |
| T10 Empty/Reformulated Search | PASS | reformulation counts as progress; identical repeat does not |
| T11 Genuine Ambiguity | PASS | unresolved scientific goal routes ASK; Controller does not choose |
| T12 Explicit GFF Without FASTA | PASS | no companion search/FASTA or annotation ambiguity |
| T13 Required FASTA Candidate Search | PASS | candidate returned but not bound |
| T14 Explicit Binding Precedence | PASS | explicit annotation persists; discovered candidate excluded from TaskSpec |
| T15 Runtime Audit Scope | PASS | only effective selected, nonexcluded, executable metadata Skills audited |
| T16 Checked Unavailable | PASS | checked/unavailable machine row is nonsemantic/nonmandatory and does not reopen semantics |
| T17 Unavailable + Block | PASS | semantic finish accepted; Planner receives `RUNTIME_STAGE_BLOCKED` |
| T18 Unavailable + Substitution | PASS | exact Skill/status/reason and allowed replacement actions returned |
| T19 Unavailable + Fail | PASS | precise terminal `RUNTIME_UNAVAILABLE`, owner runtime_preflight, execution false |
| T20 Stale Cleanup | PASS | superseded runtime row inactive/nonmandatory via set diff |
| T21 Controller Inconsistency | PASS | excluded active Skill becomes deterministic `INTERNAL_PROTOCOL`, not ASK |
| T22 Genuine No Progress | PASS | same complete actionable context terminates at 2/2 |
| T23 snpEff Progressive Flow | PASS | search → load → select/finish → Planner without empty turn |
| T24 Prompt Payload Boundary | PASS | bounded deltas, no full catalog/doc sentinel, runtime row visible once, telemetry present |

## 16. GWAS offline replay

Command:

```bash
python -m pytest -q tests/test_gwas_offline_authority_replay.py
```

Result: `1 passed in 2.08s`; failed `0`; skipped `0`. Status: `OFFLINE REPLAY PASSED`.

The replay performs no model call and generates/executes no analysis command. It deterministically verifies:

1. raw EMMAX/TASSEL mention creates no selection;
2. three Consultant queries find the GWAS workflow/QC/EMMAX/plot/LD/TASSEL candidates;
3. explicit loads still create no selection;
4. Consultant selects workflow/QC/EMMAX/plot/LD and excludes TASSEL;
5. explicit VCF/phenotype/Q/GFF inputs resolve without inferred FASTA ambiguity;
6. typed policy is `runtime_unavailable=block_stage_and_continue_closeout`, substitution false;
7. only effective selected executable Skills receive runtime evidence;
8. EMMAX unavailable is `evidence_status=checked`, not semantic unresolved;
9. semantic route is Planner; runtime route is Planner with `RUNTIME_STAGE_BLOCKED`;
10. `report`, `reproduce`, and `stage_status` remain required/plannable;
11. no generated command or scientific output is fabricated.

## 17. snpEff progressive regression

T23 uses the real local `genomics/snpeff-annotation` catalog entry and verifies:

```text
Consultant semantic search
→ candidate only
→ explicit full-doc load
→ load-before-select validation clears
→ selected Skill + TaskSpec candidate
→ checked runtime-ready evidence
→ Planner route
```

It passes inside the T01–T24 suite. There is no first-binding rejection, no `selected_skills=[]` finish acceptance, and no meaningless post-selection empty Consultant round.

## 18. Test and verification commands

| Command/scope | Passed | Failed | Skipped | Duration/result |
|---|---:|---:|---:|---|
| `py_compile` changed modules/tests + `git diff --check` | n/a | 0 | 0 | PASS |
| T01–T24 authority suite | 24 | 0 | 0 | 7.47s |
| GWAS offline replay | 1 | 0 | 0 | 2.08s |
| changed resolver cases: FASTQ mate, compatible/mismatch/multiple annotation, expected_answers exclusion | 5 | 0 | 0 | 1.22s |
| TaskSpec formal audit/idempotence/typed policy cases | 3 | 0 | 0 | 0.49s |
| existing `test_skill_manager.py` + `test_consultant_skill_discovery_repair.py` | 68 | 0 | 0 | 5.23s |
| combined authority/runtime/GWAS + existing skill suites | 93 | 0 | 0 | 11.01s |
| full `tests/test_formal_artifact_resolver.py` | 10 | 7 | 0 | 16.70s; not accepted as green, see limitations |
| `tests/test_consultation_state.py`, system Python 3.13 | 0 | collection error | 0 | 2.24s; missing `colorama` |
| same file, `/public/home/liusiqi/anaconda3/envs/biomind/bin/python` | 0 | collection error | 0 | 2.84s; LangGraph/LangChain `AnyMessage` import mismatch |

No unexecuted suite is reported as passed. The task-specific acceptance is based on the green pure/unit/offline suites above, not on the two collection-blocked environments.

## 19. Commits

| Hash | Subject | Files | Verification before commit |
|---|---|---:|---|
| `d0e2081dcf0216e6acf21ef47373b80b5f81d7a6` | `refactor(skills): make discovery queries and selection consultant-owned` | 4 | targeted discovery/manager tests and diff checks |
| `5cfcff2a4095b140b8206e97fbf24485645ad62e` | `refactor(inputs): keep semantic input binding consultant-owned` | 3 | focused resolver + TaskSpec tests and diff checks |
| `10f3cc684a50614df481851781f9fa036ef80b39` | `refactor(controller): separate semantic completion from machine evidence` | 6 | 93-test focused suite, cache/payload checks, py_compile and diff checks |
| this report/replay commit | `test(agent): add consultant authority boundary replay coverage` | 3 | T01–T24, GWAS replay, focused resolver/TaskSpec/skill suites |

The fourth commit hash cannot be embedded in the content of the commit that creates this report without changing that hash. Its exact full hash is recorded in the final terminal handoff and is available from `git log -1`.

Commit 2 also contains `task_spec.runtime_failure_policy()` because the typed policy and semantic input provenance share the canonical TaskSpec contract module. Actual runtime evidence reconciliation and routing are isolated in Commit 3. T01–T24 were kept in one auditable matrix file and committed with replay coverage in Commit 4 rather than splitting one test module across earlier commits.

## 20. Real validation not run

```text
REAL MODEL/API RUN = NOT RUN
REAL GWAS RUN = NOT RUN
FULL BENCHMARK RUN = NOT RUN
```

No provider API was called. No EMMAX/TASSEL/R environment was installed or repaired. No real GWAS command, scientific stage, full benchmark, failed-session resume or output generation was performed. `/public/home/liusiqi/03.毕设/启动.sh` and the denied `.env.api_keys` were not read or modified.

## 21. Known limitations

1. `tests/test_consultation_state.py` imports `BioAnalystAgent`; it cannot collect in the available system Python because `colorama` is absent, and it cannot collect in the biomind environment because installed `langgraph` expects `langchain_core.messages.AnyMessage`. The new pure authority tests cover the modified state/routing functions without importing the full graph.
2. The full resolver file currently has 7 failing TAD/boundary fixture-isolation cases. Their failure output shows paths under `/public/home/liusiqi/03.毕设/benchmarks/...` entering temp-root scenarios, producing `ambiguous_candidates` or wrong candidate sets. None of the 7 is one of the five changed reference/input acceptance cases, which all pass. This suite is explicitly not claimed green and the unrelated workspace search-root isolation was not widened into this refactor.
3. Offline GWAS replay validates authority, state and route semantics only. It does not prove that a real provider follows the action protocol, that Planner materializes the blocked DAG exactly, or that EMMAX is available.
4. Existing historical sessions are not migrated. This implementation reconciles current active state when the new code processes it; it does not rewrite archived run artifacts.
5. The out-of-scope terminal task/session reuse defect and broad terminal diagnostic refactor remain unchanged.
6. Runtime registry/environment repair remains out of scope. `RUNTIME_STAGE_BLOCKED` is therefore validated structurally, not through a real unavailable EMMAX run.
7. The allowlisted benchmark v3/v5 trees, benchmark harness files and four architecture audit files remain separate pre-existing work and are not part of these commits.

## 22. Next-step real-run checklist

Before a separately authorized real run:

- [ ] Pin compatible `langgraph` / `langchain_core` versions in the intended BioMind test environment and provide `colorama` in the non-runtime test environment, then collect `tests/test_consultation_state.py`.
- [ ] Run the full graph-level Consultant integration suites in that verified environment and record passed/failed/skipped/duration separately.
- [ ] Fix or isolate the existing TAD/boundary resolver search-root leakage, then rerun the complete resolver file; keep this as a separate scope/commit.
- [ ] Use a non-secret-safe launcher boundary and explicitly authorize a model-only protocol smoke test; verify the request contains bounded search/load deltas and no duplicate docs/runtime rows.
- [ ] Inspect the exact context trace for `SEARCH_SKILLS → LOAD_SKILL_DOC → select_skill` and verify the next route is runtime preflight/Planner without an empty finish turn.
- [ ] Verify the Planner converts `RUNTIME_STAGE_BLOCKED` into truthful blocked/skipped stages while retaining `report`, `reproduce`, and `stage_status` outputs.
- [ ] Only after separate authorization and a verified EMMAX runtime, run a controlled real GWAS fixture; do not reuse the failed terminal session.
- [ ] Run the full benchmark separately and keep generated run outputs outside this refactor commit chain.

## 23. Completion checklist

- [x] revised Git preflight passed
- [x] exact branch created from base HEAD
- [x] current discovery implementation recorded
- [x] raw prompt search/load/select/runtime shortcut removed
- [x] Consultant-owned multi-query discovery implemented
- [x] bounded digest-aware load and load-before-select implemented
- [x] selected/excluded typed state implemented
- [x] semantic input binding separated from filesystem candidates
- [x] runtime evidence separated from semantic completion
- [x] active-set reconciliation and owner-aware routes implemented
- [x] no-progress guard preserved at 2/2 with stronger evidence sensitivity
- [x] T01–T24 passed
- [x] GWAS offline replay passed
- [x] snpEff progressive regression passed
- [x] real API/model run not performed
- [x] real GWAS run not performed
- [x] allowlisted pre-existing paths untouched and unstaged
- [x] no push performed
