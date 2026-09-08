# BASE-01 / SF-01 / BN-02+FB-03 / RP-04 实施记录

本记录是开发修复交付，不是原科学任务的 report.md。原运行 `session_1788828826` 的源码版本仍为 **NOT_PERSISTED**。没有补写历史 checkpoint、schema 或 source revision，没有真实 API 或科学运行。

## 版本及依赖闭合

分支：`refactor/consultant-controller-authority`。原 HEAD：`57a6cafae23ab668c9d188e3ae5e7b5a7d806a02`；原 index 空。原工作树为 71 modified / 33 deleted / 393 untracked。

BASE-01 将审阅过的历史前置按依赖闭合单元提交，未应用 prerequisite-probe-not-for-commit.patch。75 个文件的逐函数归属、依赖理由及 H→C 完整差异保留在 `/tmp/biomind-base01-20260908-k_r8avrh/review/inclusion_manifest.json` 和 `review/final-safe.diff`。不是全工作树快照。

真实 store 链：Planner caller 的 capability/effect 声明 → `save_plan_state(step_effect_capability_registry=...)` → allocation 与正式引用重写 → `_prepare_atomic_plan_objects` 的 canonicalization、effects/dataflow 重编译 → approval snapshot/currentness → atomic store/CAS → 新实例 reload → 后续 admission。新测试由原始 Plan candidate 进入编译器；没有吞参数、删调用、预制正确 Plan 或替换 store/CAS。

相关的 candidate receipt、workspace/manifest、执行 provenance attestation、typed semantic completion、inspection reader、managed_stop、outer/provider/profiler/report 和现有上下文 episode/compaction 消费方一起闭合。BASE 唯一新生产适配是可选 context 配置导出采用既有默认值，缺失时不启用 compaction；未读取或提交 config。没有 Evidence Registry 迁移或恢复 Controller 路由权。

排除：所有 config/startup/凭据、33 个原文档删除、原 session/数据库/科学数据、未列入 manifest 的历史测试与文档。未纳入的安全源码另做字节保护核对；受保护配置只记录 Git 状态，不读取内容。

## 实际协议、scope 与调用链

新批准 Plan 在 Skill authoring/bind 阶段冻结 `generate_binding_input_contract.protocol=public_handle_v2` 及规则，纳入语义 hash 和批准投影。旧快照没有该字段时不迁移。模型输出语义值和批准目录的 `{handle: ...}`；系统从当前固定 invocation 的 capability/contract/catalog 得到准确 identity、kind、实际 revision、binding_id 和 Skill 声明的 binding_env。没有 Java、snpEff 名称特判、猜路径、revision=1 假设或自动更新旧 revision。

正式生产链：实际 `BioAnalystAgent` Generate → 从 approved snapshot 选当前 step、冻结 `GenerateInvocationAudit` → 同目录进入 provider messages 与 binder → 原始 JSON/parser → 公共 binding normalizer → 原 operation/workspace/full binding admission → 受校验的 concrete artifact admission → bound candidate/action hash → durable pending action → Execute 原 HITL/full manifest/currentness/prelaunch → 外部 runner → 独立 parser 与 TypedSupervisor → CommitStep CAS → accepted binding → reload → 下一步实际 Generate 请求及 consumer 绑定。

固定 scope 包含 task/revision、plan/id/version/revision、step；尚无 attempt 时明确空 attempt。晚到旧 graph step 不覆盖实际 invocation；持有的 approved Plan 副本供 rejection producer 使用，后续 context CAS/reload 不把 receipt scope 拼成“最新正确值”。CommitStep 仅在 CAS 成功后发布 successor。真正旧 scope、action/receipt 冲突及并发 CAS 继续拒绝。

normalization receipt 分开保存原始 public JSON、parser action、normalized candidate 及各 digest、固定 scope/contract/catalog digest。版本 1 receipt 仍按原格式重验。新协议的修正指纹取原始 public JSON，避免 parser 丢字段后将有效身份修正误判为同候选。显式跨 task/plan/capability 的输入先拒绝。普通 env 同值兼容，异值冲突拒绝；经过 binder 验证的环境在 admission 和 Execute 前均可见。

首轮真实 prompt 附 `binding_input_requirements`，不会依赖 serializer 丢弃的自定义字段。rejection → durable event/immutable receipt → 下一轮实际请求，包含目标类型、合法 handle 目录、consumer ref、系统 env 职责、具体错误和修正依据。旧完整对象保持严格校验，不修复错误 revision 或缺失 mapping。

后继 Generate 的语义历史只去掉 workspace attestation 的机器摘要 `content_hash`/`snapshot_digest`；durable attestation 及其语义事实保留，控制元数据 validator 未放宽。

## 两层预算与恢复

`agent_core/candidate_repair_policy.py` 提供新 scope 的唯一默认值 4，durable budgets、RecoveryPolicy 的 pre-action policy、producer/receipt 和 TypedGenerate 默认均引用它。旧 durable 冻结 2 保留，旧已停止任务不复活。初次 + 最多四次修正，共五个语义候选；同候选/无事实进展可以更早停止。

provider dispatch 前，通过原 Plan store/CAS 在现有 candidate repair ledger claim 初次授权或当前不可变 receipt。没有第二套纠错计数器。完成响应保存为 StateContractStore 对象；重复完成幂等，重载已完成响应不再调用 provider，未知 claim 不返还额度。修正 ordinal 为 0/1/2/3/4；第 5 个拒绝产生预算停止 receipt，不能产生第 6 个候选。传输重试共用一次语义授权；format repair、Skill continuation、Recovery 和 summary 使用各自原预算。

## 公共停止与总结

真实第一步 CommitStep → 第二步 Generate 耗尽（无 pending attempt）→ typed Generate outcome → managed graph boundary → outer `_run_agent_interrupt_loop` → `_finalize_managed_terminal` → `finalize_managed_stop` → durable stop、profiler、InteractiveRunResult、终端、确定性报告。

公共入口依据合法非成功终态，不以 inspection/execution 两项白名单控制。HITL、等待、取消、正常成功及非失败 END 的负例不触发失败总结。

先构造 deterministic base，再进行可选 reader 丰富、持久化和 summary claim。许可/预算满足时，实际 `_ainvoke_llm(stage=terminal_summary, task_mode=readonly)` 调用一次隔离 provider，策略单次、无工具、有界。结果验证失败/超时使用 base，错误另记；disabled/预算不足/无法确认 durable claim 时零调用并给出 skip receipt。没有把预算或 reader/store 失败说成依赖不存在。

本步事实明确 `attempt_id=''`、`executed=false`、`returncode=null`、`validation_status=not_performed`、`committed=false`。第一步已提交工作来自 CommitStep receipt 和 semantic consumption 的关联，不能借上步 returncode 伪造第二步执行。primary cause、latest failure、准确 stop reason 和 budget receipt 分开保留。

## 验收环境与边界

现有 Python 3.12.12、langgraph 1.0.7、langchain-core 1.2.7、pytest 9.1.1；未安装/换环境。`env -i` 加合成配置，全部测试产物在 `/tmp`。bootstrap audit 拒绝网络、原工作区读、启动/凭据读和科学 subprocess，仅允许既有 stdin-only `bash -n` 检查合成脚本语法，不执行脚本。

生产模型 transport 与科学 runner 为替身；Plan compiler/store/CAS、binder、admission、approval、graph、独立 contig parser、TypedSupervisor 和 CommitStep 保持真实。通用终止出口枚举用合成 typed outcome 测真实 graph edges/outer/provider，不能据此宣称每个业务 producer 都做了完整实跑。

测试 driver 使用每 10 秒只读 asyncio task observer；它不修改业务返回或断言，但可能改变事件循环唤醒时序。BASE 无 observer 的 P05 曾出现 45 秒超时或 `agent.aclose → asyncio.to_thread` 清理挂起，仍未证明修复。一次外部 faulthandler 栈转储期间 Python 进程崩溃（139），两组关联测试以 143 退出，均不计通过；最终 driver 不启用异步栈转储。候选之间的旧通过数不转移给最终提交。

已知 baseline：同一 W/C 输入均为 43 passed + 2 个旧 Skill 精确形状断言失败；未修改这些断言。recorded rep_08 的旧 blocked 期望与其协议失败 fixture 的 internal_protocol_error 不符，单独归属，保留测试并在回归选择中明确排除；没有把 4→1 次数断言改绿。

## 交付状态与真实提交

- IMPLEMENTATION_STATUS：已实施并提交；验收有下列明确缺口，不能标记全矩阵无条件 PASS。
- DEPENDENCY_BASELINE_ACCEPTANCE：隔离配置下核心前置通过，保留已知 baseline 缺陷及清理时序限制。
- SF01_STATUS / BN02_FB03_STATUS / RP04_STATUS：IMPLEMENTED_AND_COMMITTED。
- FINAL_CODE_HEAD：`f5e07350f1d27f4b3bd1666d413f1df28c06a272`；CANDIDATE_TREE_ID：`38dc2f54a699bd39ee7eac3fe78e22763f6e2f71`；COMMIT_TREE_MATCH：TRUE。
- 最后生产源码候选：`/tmp/biomind-base01-20260908-k_r8avrh/feedback-final`。本报告的文档提交不改变测试过的生产源码；最终 Git HEAD 在答复中给出。
- REAL_API_RUN=NO / REAL_SCIENTIFIC_RUN=NO / PUSH=NO。

| Commit | 逻辑范围 |
|---|---|
| `52982db200b5e43e459fdf0d4b0e02cfa3eaf6b0` | chore(integration): consolidate managed workflow prerequisites |
| `e71517a37cb684bafb96e9b85209c400ad000dae` | fix(generate): preserve invocation scope and pre-execution stop facts |
| `fff71a6811389851b9b80c5d621ad0bb24af73fa` | fix(bindings): normalize approved public handles and persist repair requirements |
| `15c794f45f0f0a1ef63379d9a6514cf833f19d2f` | fix(generate): route public binding rejection through typed repair receipts |
| `3391f99b6cf2f2b6da4eb6d25323163cd266747b` | fix(generate): persist four corrective authorizations and close binding consumers |
| `f5e07350f1d27f4b3bd1666d413f1df28c06a272` | fix(generate): include independent public violations in durable repair feedback |

RP-04 的依赖闭合还包括：原始 public JSON 修正指纹；admission 看见经 binder 校验的环境；accepted scalar 从 Execute 当前 resolver 进入 workspace 后置条件模板；后继 Generate 的 workspace 语义投影。这些都由后继 consumer 的真实调用链暴露，未把模型候选伪装成 accepted，也未从 shell/env 猜测绑定。

FB-03 最后补丁在已有拒绝上附加独立 operation、env 与父值形状检查，保留第一个原因及原校验顺序；其追加错误同样落入 event/receipt 和实际下一轮 request。模型普通 env 值未公开用于诊断，只给字段类型/目标规则。

模型原始 response 内容另由 provider claim 对应的 immutable response_ref 保存；normalization receipt 保存 JSON 语义对象及 parser/normalizer digest。这些是新调用的来源关联，不给旧 receipt 补造历史数据。

## 测试来源和实际结果

以下按各自候选报告，不跨候选相加。subtests 单列；同一场景在不同逻辑候选的回归不视作新增独立覆盖。

| Suite | 实际结果 | 源码 SHA-256 | 日志 |
|---|---|---|---|
| base-exact-P12 | 52 passed, 1 deselected, 9 subtests passed in 89.73s (0:01:29) | `47aa640891e6c015912225698a7b854fa69697eccfa569abfb9b63d9f4bb98f8` | `/tmp/biomind-base01-20260908-k_r8avrh/base-exact-P12.log` |
| base-exact-P3 | 89 passed, 11 subtests passed in 34.68s | `47aa640891e6c015912225698a7b854fa69697eccfa569abfb9b63d9f4bb98f8` | `/tmp/biomind-base01-20260908-k_r8avrh/base-exact-P3.log` |
| p05-live10 | 1 passed in 21.35s | `47aa640891e6c015912225698a7b854fa69697eccfa569abfb9b63d9f4bb98f8` | `/tmp/biomind-base01-20260908-k_r8avrh/p05-live10.log` |
| sf01-e2e | 4 passed in 84.44s (0:01:24) | `4ec62f7f2ac3fcfcf7f947d1b5e4ff516ca5ce37e6cdbdfe8eddb8986d8bfcae` | `/tmp/biomind-base01-20260908-k_r8avrh/sf01-e2e.log` |
| sf01-regression2 | 97 passed, 6 subtests passed in 43.96s | `4ec62f7f2ac3fcfcf7f947d1b5e4ff516ca5ce37e6cdbdfe8eddb8986d8bfcae` | `/tmp/biomind-base01-20260908-k_r8avrh/sf01-regression2.log` |
| bn-e2e5 | 13 passed in 102.14s (0:01:42) | `1a507dd33f2c80d82be25e8df498043c38d0c5758607423bd6009e69e7ba6cf7` | `/tmp/biomind-base01-20260908-k_r8avrh/bn-e2e5.log` |
| bn-regression2 | 80 passed, 1 deselected, 15 subtests passed in 124.07s (0:02:04) | `1a507dd33f2c80d82be25e8df498043c38d0c5758607423bd6009e69e7ba6cf7` | `/tmp/biomind-base01-20260908-k_r8avrh/bn-regression2.log` |
| bn-public-route | 2 passed in 56.81s | `d0444f36b4e5910ecb5b638de613bc46e9a3dd4c346a19342b5618c429b2f1bd` | `/tmp/biomind-base01-20260908-k_r8avrh/bn-public-route.log` |
| verified-core | 81 passed, 6 subtests passed in 204.70s (0:03:24) | `132d709d0de29ff2118218e1f4ab452ffda99ab1e5082592e0e6d5d8b5ef823b` | `/tmp/biomind-base01-20260908-k_r8avrh/verified-core.log` |
| verified-stops | 87 passed, 1 deselected, 9 subtests passed in 451.88s (0:07:31) | `132d709d0de29ff2118218e1f4ab452ffda99ab1e5082592e0e6d5d8b5ef823b` | `/tmp/biomind-base01-20260908-k_r8avrh/verified-stops.log` |
| verified-consumer | 1 passed in 44.66s | `132d709d0de29ff2118218e1f4ab452ffda99ab1e5082592e0e6d5d8b5ef823b` | `/tmp/biomind-base01-20260908-k_r8avrh/verified-consumer.log` |
| feedback-final-core | 14 passed in 336.95s (0:05:36) | `987bba3c018f7f37547ba43f870edf403f2d8dfd41584f5da7add76852f3f25f` | `/tmp/biomind-base01-20260908-k_r8avrh/feedback-final-core.log` |
| feedback-final-stops | 24 passed, 1 deselected, 9 subtests passed in 363.33s (0:06:03) | `987bba3c018f7f37547ba43f870edf403f2d8dfd41584f5da7add76852f3f25f` | `/tmp/biomind-base01-20260908-k_r8avrh/feedback-final-stops.log` |
| feedback-final-boundaries | 24 passed in 23.73s | `987bba3c018f7f37547ba43f870edf403f2d8dfd41584f5da7add76852f3f25f` | `/tmp/biomind-base01-20260908-k_r8avrh/feedback-final-boundaries.log` |
| final-wire | 2 passed in 71.00s (0:01:10) | `987bba3c018f7f37547ba43f870edf403f2d8dfd41584f5da7add76852f3f25f` | `/tmp/biomind-base01-20260908-k_r8avrh/final-wire.log` |

最终两个源码候选的确切文件清单/digest、父提交和 tree 校验见 `rp04-commit.json`、`fb03-final-commit.json`；每个相对路径均在提交前逐字节核对实际 index 内容。最终反馈补丁只改 3 个明确文件，针对其所有调用影响重新跑成功链、scope、停止/fallback、legacy recorded 与重复收尾/并发边界。

可复现调用形式（suite 的 nodeid/module 列表在对应 JSON 中）：

```text
env -i PATH=/usr/bin:/bin TMPDIR=/tmp/biomind-base01-20260908-k_r8avrh \
 BIOMIND_OFFLINE_RESULTS=/tmp/biomind-base01-20260908-k_r8avrh/recheck.json \
 /public/home/liusiqi/anaconda3/envs/biomind/bin/python \
 /tmp/biomind-base01-20260908-k_r8avrh/release_driver.py \
 /tmp/biomind-base01-20260908-k_r8avrh/feedback-final \
 <对应测试模块> -q -s -p no:cacheprovider
```

recorded 回归的选择明确包含 `-k 'not rep_08'`，原测试未改成 skip/xfail。`final-wire` 是单独列出的外部测试源 `test_wire_capture.py`，其导入的 consumer 测试与包装捕获各运行一次，不能把两次都当作不同业务覆盖。driver 和外部测试 SHA-256 见 `FINAL_IMPLEMENTATION_METADATA.json`；源文件均保留在同一临时交付目录。

## 实际 provider 消息与总结证据

`provider-context-check.json`、`final-wire.facts.json`：四次业务 Generate 请求；每次都有六步 approved Plan 语义投影、TaskSpec、当前步骤/依赖、Skill 完整继承材料及当前 capability 内容。第二步 initial 与 corrective 请求的前三条冻结 Seed 消息逐项完全相同，后接 candidate 和具体反馈。第三步 runtime 区含两个 accepted binding，值与实际 CommitStep/reload 一致。

本合成 fixture 没有完整 Consultant handoff，因此这项完整 handoff 可见性为 NOT_EXERCISED，不声称已经核验原 session 的上游全文。也不把 Plan 的六步语义投影说成所有内部控制字段原样发送。

捕获 provider.bind 只出现 summary 初始化的 `max_tokens=1500`；本 Generate 调用没有观察到 `response_format`、tool schema 或强制 JSON Schema。实际约束是文本 semantic-action JSON 协议和原 parser/admission；不能用 token 数代替字段可见性证明。

| 故障/许可场景 | 实际 summary 调用 | 结果 |
|---|---:|---|
| 许可且正常 | 1 | COMPLETED；base 与报告保留 |
| provider 异常 / timeout | 各 1 | FAILED；base 保留并附具体错误 |
| disabled / budget 不足 | 各 0 | SKIPPED；分别的 skip receipt |
| reader 故障 | 1 | base 不依赖 reader；详情错误保留 |
| store/claim 无法确认 | 0 | 内存 base，persistence=unconfirmed，无假 durable stop/report |
| writer 故障 | 1 | stop/base 保留，report receipt=failed，无伪报告路径 |
| HITL/取消/正常成功/等待 END | 0 | 不创建失败收尾 |

`delivery-receipts.json` 列出真实断言时创建的报告 receipt 与保留副本。成功、summary error、disabled、timeout、budget、reader 六组的 report.md / stage_status.tsv / reproduce.sh 在 `rp04-deliverables/<mode>/` 可查。原测试 fixture 路径在 teardown 后删除，副本与 receipt 明确保留；这些不是原 session 的科学交付。writer 故障的最终 `.facts.json` 明确无报告路径；store 故障以测试的内存 base/零调用断言为证，没有伪造 durable 文件。

## 原规格逐项验收矩阵

PASS 表示所列层级的断言通过；不得把 normalizer 单元测试解释成完整 graph。PARTIAL 是明确的剩余验收缺口。

| ID | 要求 | 结果 | 实际证据/边界 |
|---|---|---|---|
| S01 | actual invocation 第二步、入站 graph 第一步 | PASS（见覆盖层级） | 实际第一步 CommitStep→第二步 Generate，含旧入站 step 与无 attempt；scope/five_candidate_stop |
| S02 | 没有 pending_action / attempt | PASS（见覆盖层级） | 实际第一步 CommitStep→第二步 Generate，含旧入站 step 与无 attempt；scope/five_candidate_stop |
| S03 | 真正旧 step outcome 迟到 | PASS（见覆盖层级） | 真实 admission CAS race/late outer、typed scope/receipt guard、CommitStep CAS/successor 测试；不同层级覆盖 |
| S04 | invocation/receipt/action 身份冲突 | PASS（见覆盖层级） | 真实 admission CAS race/late outer、typed scope/receipt guard、CommitStep CAS/successor 测试；不同层级覆盖 |
| S05 | CommitStep CAS 与 successor 投影 | PASS（见覆盖层级） | 真实 admission CAS race/late outer、typed scope/receipt guard、CommitStep CAS/successor 测试；不同层级覆盖 |
| S06 | 原案例三层诊断 | PASS（见覆盖层级） | 实际第一步 CommitStep→第二步 Generate，含旧入站 step 与无 attempt；scope/five_candidate_stop |
| S07 | pre-action 停止事实 | PASS（见覆盖层级） | 实际第一步 CommitStep→第二步 Generate，含旧入站 step 与无 attempt；scope/five_candidate_stop |
| S08 | 第一步进度 | PASS（见覆盖层级） | 实际第一步 CommitStep→第二步 Generate，含旧入站 step 与无 attempt；scope/five_candidate_stop |
| S09 | 正确 scope 总结成功 | PASS（见覆盖层级） | 实际 graph/outer/provider；成功、异常、超时、disabled、预算不足、reader/store/writer 故障 |
| S10 | summary 异常或超时 | PASS（见覆盖层级） | 实际 graph/outer/provider；成功、异常、超时、disabled、预算不足、reader/store/writer 故障 |
| S11 | disabled、预算不足 | PASS（见覆盖层级） | 实际 graph/outer/provider；成功、异常、超时、disabled、预算不足、reader/store/writer 故障 |
| S12 | store/claim 失败 | PASS（见覆盖层级） | 实际 graph/outer/provider；成功、异常、超时、disabled、预算不足、reader/store/writer 故障 |
| S13 | reader/writer 失败 | PASS（见覆盖层级） | 实际 graph/outer/provider；成功、异常、超时、disabled、预算不足、reader/store/writer 故障 |
| S14 | 重复 finalizer / summary reservation 未知 | PASS（见覆盖层级） | common_finalization_workflow 重复 finalizer + common_finalization_faults 未知 cleanup；无重复调用 |
| S15 | HITL 暂停、取消、正常成功、子图 END | PASS（见覆盖层级） | 31 正终止路由之外的 HITL/等待/取消/成功负例，经真实 graph/outer |
| B01 | 新 handle 合法且无机器字段 | PASS（见覆盖层级） | 真实生产 Generate/binder/admission/approval/runner/verifier/CommitStep；consumer Generate 重载，env collision 负例 |
| B02 | catalog revision 非 1 | PASS（见覆盖层级） | 通用 normalizer 层：不同 capability/revision=7、未知/歧义/混合/不支持协议；未知 handle 另有 graph 修正 |
| B03 | 未知/歧义 handle | PASS（见覆盖层级） | 通用 normalizer 层：不同 capability/revision=7、未知/歧义/混合/不支持协议；未知 handle 另有 graph 修正 |
| B04 | 完整跨 task/plan/capability 输入 | PASS（见覆盖层级） | 真实 Generate caller 的跨 task/plan/capability 候选拒绝后合法修正；另有 raw 字段 parser 丢弃负例 |
| B05 | catalog 在调用后变更 | PARTIAL | stale/revision/currentness/normalization receipt guard 已验证；未另做“Generate 后合法 catalog revision 更新”这一精确综合场景。 |
| B06 | 合法 legacy 完整对象 | PASS（见覆盖层级） | 原 recorded/full binding/workspace 测试及 legacy normalizer 分支；旧审批不迁移，错 revision/缺 mapping 保留拒绝 |
| B07 | 旧字符串、{artifact_ref:path}、错 revision | PASS（见覆盖层级） | 原 recorded/full binding/workspace 测试及 legacy normalizer 分支；旧审批不迁移，错 revision/缺 mapping 保留拒绝 |
| B08 | protocol/version 不受支持或混合输入 | PASS（见覆盖层级） | 通用 normalizer 层：不同 capability/revision=7、未知/歧义/混合/不支持协议；未知 handle 另有 graph 修正 |
| B09 | produced/accepted 区分 | PASS（见覆盖层级） | 真实生产 Generate/binder/admission/approval/runner/verifier/CommitStep；consumer Generate 重载，env collision 负例 |
| B10 | producer 完整执行与提交 | PASS（见覆盖层级） | 真实生产 Generate/binder/admission/approval/runner/verifier/CommitStep；consumer Generate 重载，env collision 负例 |
| B11 | 新协议派生 env | PASS（见覆盖层级） | 真实生产 Generate/binder/admission/approval/runner/verifier/CommitStep；consumer Generate 重载，env collision 负例 |
| B12 | 模型 mapping 冲突及 env collision | PASS（见覆盖层级） | 真实生产 Generate/binder/admission/approval/runner/verifier/CommitStep；consumer Generate 重载，env collision 负例 |
| B13 | normalized internal 缺 required mapping | PASS（见覆盖层级） | 原 recorded/full binding/workspace 测试及 legacy normalizer 分支；旧审批不迁移，错 revision/缺 mapping 保留拒绝 |
| B14 | 任意普通 env 与科学语义值 | PASS（见覆盖层级） | 语义 genome 值、普通 env 与代码按原值保留；consumer 带批准 workspace 构建日志，旧 purpose 启发式限制另列 |
| B15 | raw/normalized provenance | PASS（见覆盖层级） | 真实生产 Generate/binder/admission/approval/runner/verifier/CommitStep；consumer Generate 重载，env collision 负例 |
| B16 | HITL/full manifest/prelaunch | PASS（见覆盖层级） | 真实生产 Generate/binder/admission/approval/runner/verifier/CommitStep；consumer Generate 重载，env collision 负例 |
| B17 | 非 snpEff 的合成 capability | PASS（见覆盖层级） | 通用 normalizer 层：不同 capability/revision=7、未知/歧义/混合/不支持协议；未知 handle 另有 graph 修正 |
| B18 | 未迁移 Skill/旧批准快照 | PASS（见覆盖层级） | 原 recorded/full binding/workspace 测试及 legacy normalizer 分支；旧审批不迁移，错 revision/缺 mapping 保留拒绝 |
| F01 | 父形状字符串错误 | PASS（见覆盖层级） | 新反馈补丁：parent + operation + env 独立错误→durable receipt→下一实际请求；五候选末次错误→报告 |
| F02 | 多个独立可查错误 | PASS（见覆盖层级） | 新反馈补丁：parent + operation + env 独立错误→durable receipt→下一实际请求；五候选末次错误→报告 |
| F03 | 新协议错误指导 | PASS（见覆盖层级） | 新反馈补丁：parent + operation + env 独立错误→durable receipt→下一实际请求；五候选末次错误→报告 |
| F04 | rejection→receipt→下一 request | PASS（见覆盖层级） | 新反馈补丁：parent + operation + env 独立错误→durable receipt→下一实际请求；五候选末次错误→报告 |
| F05 | 最后一轮错误 | PASS（见覆盖层级） | 新反馈补丁：parent + operation + env 独立错误→durable receipt→下一实际请求；五候选末次错误→报告 |
| F06 | 系统 prompt / Skill / 实际 schema | PASS（见覆盖层级） | 真实 messages + 外部捕获 bind 参数；文本 JSON 协议，不声称 wire JSON Schema；Skill 冻结规则同源 |
| F07 | 同候选/同有效输入重复与实质修正 | PASS（见覆盖层级） | public_valid_facts、相同候选提前停止、实际五候选逐项修正；没有以随机字符串变化授予进展 |
| R01 | 生产构造 policy4/adapter4 | PASS（见覆盖层级） | 唯一新默认4；初次0、第五成功、五失败停止，provider claim ordinal 0..4，无第六候选 |
| R02 | 初次即成功 | PASS（见覆盖层级） | 唯一新默认4；初次0、第五成功、五失败停止，provider claim ordinal 0..4，无第六候选 |
| R03 | 初次+四次修正，第五候选成功 | PASS（见覆盖层级） | 唯一新默认4；初次0、第五成功、五失败停止，provider claim ordinal 0..4，无第六候选 |
| R04 | 五候选全失败 | PASS（见覆盖层级） | 唯一新默认4；初次0、第五成功、五失败停止，provider claim ordinal 0..4，无第六候选 |
| R05 | resume 重建 store/adapter | PASS（见覆盖层级） | real CAS 双竞争者、immutable response 重放；第三候选调用完成前中断，重建 adapter 两次，未知 claim 不返还 |
| R06 | duplicate rejection / 并发 CAS | PASS（见覆盖层级） | real CAS 双竞争者、immutable response 重放；第三候选调用完成前中断，重建 adapter 两次，未知 claim 不返还 |
| R07 | 压缩/error/request/epoch 变化 | PARTIAL | 授权绑定 durable scope/receipt，request ID 变化与重建不重置已测；未强制 compaction/error/epoch 全组合重放。 |
| R08 | provider 传输重试 | PARTIAL | 原 transport 独立重试机制保留，调用中断实验观察过 wrapper 重试；未把传输故障叠加五候选做独立正式验收。 |
| R09 | format repair、Recovery、summary 预算 | PASS（见覆盖层级） | 旧冻结2原测试、format/Recovery 独立路径、summary一次或零调用；未改原停止任务 |
| R10 | 旧冻结额度2/已停止原任务 | PASS（见覆盖层级） | 旧冻结2原测试、format/Recovery 独立路径、summary一次或零调用；未改原停止任务 |
| G01 | 候选提交可复现性 | PASS（见覆盖层级） | 每逻辑候选独立源码指纹/正常提交/staged tree逐文件核对；原 session 未写，未知历史 revision 未补造 |
| G02 | 源码/原证据保护 | PASS（见覆盖层级） | 每逻辑候选独立源码指纹/正常提交/staged tree逐文件核对；原 session 未写，未知历史 revision 未补造 |

## schema-2 公共终止路由矩阵

以下 31 个合法非成功 typed 出口以合成 producer outcome 经过真实生产 graph/outer/provider；不改变业务停止条件，也不将它们的节点内部科学逻辑视为实跑。
| 节点 | outcome | 验收 |
|---|---|---|
| generate | candidate_rejected | durable stop + 一次 summary + outer stage/error |
| generate | provider_failed | durable stop + 一次 summary + outer stage/error |
| generate | internal_protocol_error | durable stop + 一次 summary + outer stage/error |
| generate | blocked | durable stop + 一次 summary + outer stage/error |
| generate | harness_error | durable stop + 一次 summary + outer stage/error |
| generate | runtime_recovery_stopped | durable stop + 一次 summary + outer stage/error |
| execute | pre_execution_rejected | durable stop + 一次 summary + outer stage/error |
| execute | blocked | durable stop + 一次 summary + outer stage/error |
| execute | harness_error | durable stop + 一次 summary + outer stage/error |
| recovery | blocked | durable stop + 一次 summary + outer stage/error |
| recovery | internal_protocol_error | durable stop + 一次 summary + outer stage/error |
| recovery | harness_error | durable stop + 一次 summary + outer stage/error |
| recovery | runtime_recovery_stopped | durable stop + 一次 summary + outer stage/error |
| supervisor | harness_error | durable stop + 一次 summary + outer stage/error |
| commit_step | harness_error | durable stop + 一次 summary + outer stage/error |
| final_audit | task_incomplete | durable stop + 一次 summary + outer stage/error |
| final_audit | entry_rejected | durable stop + 一次 summary + outer stage/error |
| final_audit | harness_error | durable stop + 一次 summary + outer stage/error |
| consultant | terminal | durable stop + 一次 summary + outer stage/error |
| consultant | harness_error | durable stop + 一次 summary + outer stage/error |
| planner | terminal | durable stop + 一次 summary + outer stage/error |
| planner | harness_error | durable stop + 一次 summary + outer stage/error |
| plan_review | terminal | durable stop + 一次 summary + outer stage/error |
| plan_review | harness_error | durable stop + 一次 summary + outer stage/error |
| install_subagent | terminal | durable stop + 一次 summary + outer stage/error |
| install_subagent | harness_error | durable stop + 一次 summary + outer stage/error |
| triage | harness_error | durable stop + 一次 summary + outer stage/error |
| step_worker | blocked | durable stop + 一次 summary + outer stage/error |
| step_worker | harness_error | durable stop + 一次 summary + outer stage/error |
| triage | resume_classified | durable stop + 一次 summary + outer stage/error |
| ask_user | harness_error | durable stop + 一次 summary + outer stage/error |

负例：Generate/Execute/worker cancelled；Execute lightweight_completed；plan_review approval_pending；final_audit task_verified；Consultant answer_ready/正常 terminal；真实 graph interrupt/人类通道不可用；AskUser answer_recorded/question_missing 的等待 END；caller CancelledError。均无失败 summary 和 durable failure stop。

## 剩余 Git 与未完成项

生产代码提交后 index 为空；剩余状态为 28 modified / 33 deleted / 363 untracked。完整名称列表：`/tmp/biomind-base01-20260908-k_r8avrh/git-after-code.txt`。61 个未纳入提交的安全源码与原冻结参考哈希一致，无漂移。原 33 项删除仍保留；本轮未对配置/凭据进行内容读取或哈希，因此不冒称已经独立验证其全部字节。所有落地主工作区的源码 delta 均要求原文件先与上一实际候选完全一致；未知或受保护文件不在 patch/index 清单。

1. B05：未单独验收“Generate 完成后，合法并发 catalog revision 更新，再到实际 prelaunch”这一精确组合；已有 scope/CAS/revision/tamper/receipt guard 的分层负例不能冒充它。
2. R07/R08：未强制执行压缩/epoch/error 的全部重放组合，也没有将传输故障叠加第五候选完成独立综合验收；现有预算及 transport 机制未放宽。
3. 完整 Consultant handoff 不在这套合成 fixture 中，未证明其全文实际可见；历史运行 revision 仍 NOT_PERSISTED。
4. 旧 `_pending_action_purpose` 会把带只读检查、没有显式输出的 build 脚本误判为 probe。本轮未重构该科学意图启发式；consumer 正例使用合法 workspace 构建日志输出，原先不带日志的失败证据保留。不能据此承诺任意合法 build 脚本均可通过旧 purpose 分类。
5. BASE 的无 observer 清理超时/线程唤醒问题未解决；本轮解释器栈转储崩溃与测试缩进导致 guard 拒绝 subprocess 的中间失败均保留，不纳入通过数。没有放宽 guard 去运行科学脚本。
6. 既有精确 Skill 形状断言、rep_08 期望差异和原 4→1 次数断言未被改绿；未宣称全仓测试、真实环境、真实模型/科学 benchmark 通过。额外 runtime 专项测试曾因外部测试依赖缺失而 collection error，未记 PASS；未顺带实施其他 RT/provenance 任务。

DEPENDENCY_CLOSURE_RESULT：本次 BASE/SF/BN/FB/RP 主链已闭合且有真实逻辑提交；TARGET_ACCEPTANCE_RESULTS：核心成功/有界停止/收尾与隔离安全验证通过，上述剩余验收项保持 PARTIAL/NOT_RUN，不补造证据。
