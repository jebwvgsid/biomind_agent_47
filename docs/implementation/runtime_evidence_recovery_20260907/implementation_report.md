# Runtime evidence recovery implementation

STATUS: IN_PROGRESS — RT-01 validated; RT-02/RT-03 pending.

Original HEAD: b9a686445a1f4c9f3c7b94bcd55f70ea9998efa5
Branch: refactor/consultant-controller-authority
Initial Git: 71 modified / 33 deleted / 56 untracked status entries; staged none.

Previous mutation provenance patch: PRESENT_UNCOMMITTED; SAFE_ATOMIC_COMMIT_PREVIOUS_PATCH=BLOCKED. Its registration reducer requires the uncommitted workspace_attestation framework and action.status=bound; its Execute journal producer depends on prepared_mutation_intents and dirty workspace admission. Its regression fixtures additionally depend on untracked semantic_completion, step_effects and context_compaction. None were absorbed as a baseline.

Necessary isolated prerequisite: e4f01f9969425a07b0903abff1917c19f90ac784. HEAD's production Planner still rejected the schema-2 handoff using its legacy FINALIZE gate. The small gate change uses existing durable consultant_planning_readiness; no new Controller routing authority. Five production gate tests passed on HEAD plus that delta alone. The original working-tree implementation was preserved.

RT-01 fixes all three actual Consultant return/commit seams, immutable task-owned evidence acceptance, scope-aware reload and production binding consumption. A legacy PackageManager summary cannot be silently resealed as current scoped proof: typed preflight restores compatible task evidence or performs its existing real inspection. Reuse preserves original observation time; metadata-only summaries are not observations.

Production transport test: real schema2_app load_skill_doc → select_skill → update_taskspec (revision increases) → explicit handoff → real Planner persistence → new BioAnalystAgent → durable reload → registry → generic runtime.tool.bin resolver. Three Consultant commits succeed and only one fake inspection is dispatched. The test also rejects a forged graph audit without the graph schema field and invalidates a document changed beneath a live SkillManager. No target producer/commit/classifier is mocked.

Validation: 31 tests passed on the RT-01 candidate; the independent Planner prerequisite has 5 passing cases. Sixteen record tests include expiry, missing scope, unknown/error distinction, conflicting and superseding observations, immutable IDs, partial coverage, sensitive fields and a negative fact that would otherwise be bypassed by existing Java HOME derivation. Fourteen existing action-runtime tests pass with their previously dirty three-line assertion correction: HEAD's provider already emits concrete values, while its stale test expected $TOOL_BIN. That assertion change is explicitly included; no production provider contract is changed.

Candidate version: e4f01f9969425a07b0903abff1917c19f90ac784 + RT-01 source sha256:a60d70c3d9c9673a7aa25240f8b177994fce6ae7f01869839f296bbcb7fa7750
Candidate tree: /tmp/biomind-runtime-20260907-75abk0ij/candidate
Tests use existing /public/home/liusiqi/anaconda3/envs/biomind/bin/python (3.12), real LangGraph/LangChain and isolated temporary files. No activate/install/environment modification. The offline runner stubs only dotenv/config.startup bootstrap and forbids original-workspace reads, sensitive/startup files, subprocesses and network connections. Actual provider and inspection boundaries are fakes. Candidate import identities and stable start/end source hashes are logged.

Safety: no explicit credential/user launch-script reads, real model/API calls, real scientific execution, original-session modification, install/download, environment replacement or push. Early read-only subagent commands used a default login shell and emitted system profile banners; this was corrected to login:false. We cannot describe those early calls as having bypassed all shell profiles. All application tests use the guarded no-startup bootstrap.

RT-01 commit does not claim full G2–G5 acceptance. The 55-row matrix distinguishes passing data/transport tests from pending acquisition, checkpoint, Execute/HITL, provenance and final-report tests. E08 remains blocked for a HEAD-only candidate by the specifically identified historical framework dependency; it is not counted as passed merely because the dirty checkout contains it.

Real model/API: NOT_RUN. Original session_1788759228: NOT_MODIFIED. Real scientific workflow: NOT_RUN. Real runtime probe in implementation tests: NOT_RUN. Push: NOT_RUN.
