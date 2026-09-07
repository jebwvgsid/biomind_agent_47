# RT-02/03 生产图验收记录（候选草稿，非最终提交验收）

本记录对应 `/tmp/biomind-runtime-20260907-75abk0ij/candidate/tests/test_runtime_evidence_recovery_production.py` 的 9 个用例。最近合并验收包含本文件 9 个生产图用例及另 6 个 outer scope/status 用例，结果为 **15 passed in 62.21s，OFFLINE_CANDIDATE_UNCHANGED=True**。当时测试文件 SHA-256 为 `9dbfead7a6c4488818024a86720dd0f3d6dea591cff1b30f7304097685eca4bd`。后续尚未合并或提交的 service/report 等修改不得继承该 PASS；最终提交前须对最终候选重跑。

真实标准输出：`/tmp/biomind-runtime-20260907-75abk0ij/evidence/rt02-scope-and-production.log`。

源码清单及实际导入模块来源：同目录 `rt02-scope-and-production.source-identity.json`、`rt02-scope-and-production.module-origins.json`，均由真实 stdout 中的结构化记录提取。

本文件独立运行命令（上面的合并日志另包含 6 个 outer scope/status 用例）：

```sh
env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 /public/home/liusiqi/anaconda3/envs/biomind/bin/python /tmp/biomind-runtime-20260907-75abk0ij/candidate/tests/offline_runtime_evidence.py tests/test_runtime_evidence_recovery_production.py -q -x
```

复用已安装 Python 及测试依赖，不 activate、下载或安装环境。离线 bootstrap 隔离 .env、startup、网络和外部进程；临时 Skill、环境目录、registry/cache/archive、科学输出全部位于独立临时目录。真实用户工作树、原始 session 和凭据均未参与测试导入或修改。

## 真实调用链

9 个用例共用实际 `BioAnalystAgent` 构造器、临时 `SkillManager` 与 PlanTracker。`_persist_user_submission` 写入 U1 后，实际 schema-2 图经过 Triage、Consultant `load_skill_doc → select_skill → submit_planning_handoff`、`_commit_consultant_update`、durable TaskSpec/runtime evidence、Planner、PlanReview 和合法 Plan approval。初始 Consultant 检查仅产出可信 `runtime.tool.bin`；当前 report capability 同时 required `runtime.tool.bin` 与 `runtime.helper.bin`，后者仍无证据。

在运行图中，实际 Generate 于 provider 调用前执行 `_runtime_binding_source → build_runtime_binding_registry → resolve_action_runtime_bindings`；缺 helper 时创建并持久化 runtime request。`TypedGenerate.runtime_evidence_required → runtime_graph Recovery → Schema2RecoveryNode.runtime_recovery → _recover_runtime_evidence → RuntimeEvidenceRecovery.recover → RuntimeInspectionAdapter.inspect → BubblewrapRuntimeProbe.run` 经过真实检查授权、输入范围、只读 namespace argv 和输出解析。仅最末 `probe.process_factory` 为测试替身，返回真实格式的 stdout StreamReader；没有替换 adapter、service、durable acceptance、typed classifier 或 graph route。

检查产出经真实验证、PlanTracker CAS 接纳和磁盘重读后，`runtime_evidence_ready → 同一步 Generate` 重组 prompt；fake provider 返回符合原 script_block_v1 的 action，保留 managed artifact/action validation、原 code_review HITL interrupt/resume、manifest/approval identity 与 Execute prelaunch。只有最终 `run_script_path` 是科学 runner 替身。其 lifecycle callback 和返回值经过真实 Execute evidence、Supervisor、CommitStep。

## 9 个用例与 Spec ID

| 用例名（均在上述文件） | 直接断言 | Spec 对应与边界 |
|---|---|---|
| `test_baseline_real_generate_execute_hitl_and_commit_first_nonruntime_step` | 无 required runtime 的 prepare 步经过真实 Generate/HITL/Execute/CommitStep；runner 一次；current step 推进到 report | B09、E04 前半段、F08 的既有 schema-2 无 runtime happy path；不声称 schema-1 覆盖 |
| `test_missing_fact_recovers_in_same_graph_before_provider_then_executes_once` | 同一图先真实 CommitStep prepare，再 report 缺 helper；typed dispatch_state=not_dispatched；probe 一次；prepare/report 各 Generate provider 一次、各科学 runner 一次、各真实代码审批一次 | B02、B08、C01、C04、C10、E03–E06；required 两个通用 facts，不加 Java 分支、不改 optional |
| `test_real_checkpoint_interrupt_after_acceptance_resumes_same_generate` | Recovery 后真实图 interrupt；此时 Generate provider=0、runner=0、graph.next=Generate；同 checkpoint resume 后执行一次且不增 probe | C01、C04、E03、E06；是已完成 Recovery 节点后的真实 LangGraph checkpoint 中断/恢复，**不冒充 D08 的“检查完成但该 Recovery checkpoint 尚未提交”崩溃窗口** |
| `test_new_instance_and_new_graph_reopen_durable_accepted_evidence` | 同一单例先 prepare 真实 CommitStep，再 report missing→Recovery durable acceptance；重新构造 full agent，从磁盘重建 helper registry，新图/新 MemorySaver/新 thread；plan ID/version/current step/capability 一致，prepare/report 各 provider/runner 一次，总 probe 一次 | 严格第 18.1 节单例；A04、B02、C02/C04、E03–E06；不把旧 MemorySaver 重用当作磁盘重开 |
| `test_inspection_failure_stops_with_truthful_explanation_without_dispatch` | 真实检查 leaf 超时；有界停止；helper UNKNOWN、旧 tool AVAILABLE；provider/runner/CommitStep 均无；outer status=failed，failure_code/正文保留 timeout；fresh agent+empty snapshot 重读相同说明 | B06 的 timeout、C05、D04 的有界 transient 路径、F01、F05 的确定性说明可达；未覆盖 permission、crash、用户取消的全部变体 |
| `test_successful_inspection_with_incomplete_output_remains_unknown` | healthcheck rc=0 且 stdout=OK；不造 helper 路径；停止解释有 incomplete，helper UNKNOWN；已有 tool 不丢；outer 与新实例说明相同；无 provider/runner/CommitStep | B04、B05 的通用逐 fact 独立性、C05、D01 的本缺失路径不再空转 rebuild、F01；不声称覆盖 Java/JAR 既有特殊派生路径 |
| `test_execute_prelaunch_rejects_path_invalidated_during_real_approval` | 补证据、Generate 后到真实 code_review interrupt；仅删除临时 helper；批准恢复后精确 `pre_execution_rejected / RUNTIME_BINDING_PRELAUNCH_PATH_INVALID`；runner=0，原 manifest hash 不重算洗白 | E02 的路径失效、E06；manifest 内容篡改/其他 digest drift 由相应单元或既有契约测试承担 |
| `test_runtime_stop_writes_requested_reports_through_real_output_contract` | User U1 明确请求 temp report.md/stage_status.tsv/reproduce.sh；真实 Consultant output contracts 与 Planner approval；runtime incomplete STOP 后真实 reporter 写 3 文件；helper UNKNOWN、目标未完成、无 verified 科学交付物、无 provider/runner，reproduce 只描述未完成且 exit 1 | F01、F04 的无科学完成误报、F05、F06；本例未覆盖“前一步已有成功交付物”的所有报告内容变体 |
| `test_second_prelaunch_rejection_closes_unlaunched_starting_attempt` | 仅在真实 Path.touch(command_trace.log) 完成后删除 helper，首个校验通过而 dispatch 前第二次校验拒绝；runner=0，唯一 attempt=FAILED，无 pid/pgid/returncode/exit_status 文件，旧 manifest 不变 | E02 晚期路径失效及真实执行状态收口；validator、PlanTracker event/save 均为生产实现 |

A01/A02 的真实 producer/commit/Planner 链在这 9 个用例中均经过，但完整 audit return、同 ID 传递断言以 RT-01 `test_runtime_evidence_transport.py` 为准；A03 的独立 update_taskspec operation 也由该 RT-01 测试承担。这里没有将经过链路等同于新增完整验收。

所有用例的执行边界和来源记录支持 F10 的隔离候选证明。F09 的禁止凭据/网络/原工作树读取由离线工具执行约束支持；完整恶意输出脱敏矩阵仍由 adapter/report 专项测试承担。

B01 无任何 audit、B03 明确负面、B07 冲突、B10 旧 runtime preflight；A05–A07；C03/C06–C09；D02/D03/D05–D10；E01；F02/F03/F07 及 schema-1 F08，不应仅凭这 9 个测试标记全覆盖。请结合 helper、service、inspection、reporting、既有 action/typed compatibility 测试映射逐项补齐。

## 持久化层级与第 18.1 节

- 同 execution：two-step 用例使用同一图和同一 MemorySaver；真实磁盘 PlanTracker 在每次加载/接纳后重读，不重跑 prepare。
- 真正新对象/存储重开：另一个用例重新构造 BioAnalystAgent，重新读取 plan_state 文件，使用新的图、saver 和 thread ID。旧 checkpoint 对象完全不复用。
- 真实 checkpoint 接缝：第三个用例在 Recovery 节点完成后中断并 resume；这与新实例磁盘重开分开验证。
- 严格第 18.1 节已经由 `test_new_instance_and_new_graph_reopen_durable_accepted_evidence` 一个完整两步用例覆盖：真实前一步 CommitStep、两个 required facts 的当前步骤补证据、durable acceptance、新 agent/新图/新 saver 磁盘重开、同一 plan/step/capability、原 HITL/Execute prelaunch、每个 step runner 一次。相邻的同 execution 与 checkpoint 用例仍独立保留。

## E07/E08 未覆盖的精确边界

安全候选以已提交 HEAD 源码导出构建，没有借用用户 dirty/untracked 版本的 workspace/semantic 框架。

- E07：历史 malformed `snpeff_db` 与正确 `mutable_workspace` 的完整 admission/provenance 集成路径依赖历史未提交的 workspace framework。候选没有该整套 mutable workspace capability、binding/action status 与相应语义 fixture；本测试中的一般 artifact gate 通过不能替代这两类历史回归。
- E08：上一轮 mutation provenance regression `test_mutation_provenance_integration_fix.py` 依赖未提交 `workspace_attestation.py` 中整套 workspace attestation/intent/receipt 实现、PlanTracker mutation event 与 Execute journal producer 的配套 dirty hunks。Stage 0 已确定 PlanTracker event 对 action.status=`bound` 等 HEAD 不具备状态的依赖；该测试还依赖未提交 semantic_completion/context_compaction 测试夹具及 HEAD saver 不具备的 `step_effect_capability_registry` 参数。仅抽取 mutation 的少量行不能在已提交基线独立导入/运行。
- 因此 E07/E08 均为明确未完成的历史隔离依赖，不记 PASS、不声明 SKIP 代替验收，也不将本轮 runtime patch 与这些未隔离内容混合提交。

真实模型/API、真实 namespace 外部进程、实际科学运行、实际 Java/snpeff/benchmark 均 NOT_RUN；预期只运行上述允许的离线替身叶子。


## 明确保留的实现边界

- D06：本轮尚未实现 runtime LLM 等价动作/多策略循环。当前采用已声明且获准的唯一确定性 inspection producer；在没有合法可选策略时，不调用模型制造策略或许可。现有一般 Recovery agent 机制保留，但不能把其独立测试记作本轮 runtime 策略循环已经实现。
- C03：当前没有可复用且可验证 scope/observed_at 的 compatible cache producer；旧全局 PackageManager cache 被拒绝直接接纳，不能伪装成 fresh task-scoped cache adoption。本轮完成 fresh inspection 和 durable evidence 重读，C03 新鲜兼容缓存接纳仍为明确未实现的扩展。
- E07/E08：仍未纳入当前 HEAD 可独立验收的候选；具体历史 dependency/hunk 障碍见上节。候选框架隔离证明与新 runtime 路径 PASS 不会替代这两个未完成项。

最终统一候选验收以 evidence/rt02-rt03-final-candidate.log 为准：152 passed、139 subtests passed；上文分阶段日志只说明调试时的对应版本。最终源码摘要：f1eea7a12784567f6682d25254e6bc90d65b0f6a8494397dfff7884f5b8b7aa3。
