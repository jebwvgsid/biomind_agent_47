# Generate 协议、进展与终态交付：类别修复

这是原 A1/F1—F6 与 V2 的局部实施补充，不扩大原机制 PASS 或真实科学验证范围。原事故前两步已 Commit，prepare 没有执行，不能称为建库失败。候选缺 operation、把实际值填进 binding_env 是真实协议错误；第二份补 operation、合法改为 argv-only 后，旧代码漏记语法 PASS 并据此提前停止，是框架误停。三个已有报告的归属仍未证实，拒绝覆盖是合理保护，不能把旧文件当本次交付。

**IMPLEMENTATION_STATUS: COMPLETE（限本单离线类别修复与机制验收）**。生产提交 `5bc79f2cc42eccaeb17caad34cea9c23f989c116`，父版本为下述 BASE。198 个最终 pytest ID、5 场真实 PTY/plain 均通过，分组覆盖与限制见本目录 `verification.json` / `commit_files.tsv`。主入口仍是[原实施主单](../README.md)。

## 源码与净修改

BASE `4e896f7a47a2c858fd4e2087c89e34759afc58ef`，分支 `refactor/consultant-controller-authority`，加原保护 overlay。最终测试导出 460 文件，signature `02a55fe0c980c389edb2c8865bfaa98fa7e61d706b8894f13f165c9c16f8f712`。源码、公开资源、测试导出及逐进程实际加载分别记在 `manifest_and_protection.json.gz`，导出文件不等于每个函数均被执行。

净修改为 10 个生产文件、3 个新增测试文件、1 个旧测试的旧进展政策断言。相交的 `prompts/manager.py` 既有 HASH_OUTPUT_POLICY 与 `managed_stop.py` 两处中文终态说明保留在工作树，没有纳入本轮提交。其余受保护源码、测试及 index 内容逐项核对；敏感配置仅检查 Git 元数据。**验证对象是 commit + 登记 overlay，不声称裸 commit/干净克隆可以独立部署。**

| 范围 | 实际改变 |
|---|---|
| 公开字段 | 默认示例和三种 Generate 请求明确省略 binding_env；正式值仍归 produced_bindings。沿冻结规则派生来源映射，批准前绑定有效环境。显式错误值、错误 source、额外变量、null/list 仍返回 actual/expected/可省略说明。旧已批准载体和显式 legacy 完整对象的严格兼容路径不重解释。 |
| 执行载体 | `executable_carrier` 提取现有明确解释器的实际操作数，helper 物化与语法检查共用此入口。code、code+argv、argv-only 和明确 wrapper 都检查实际载体；实际对象、规则、语言、内容 hash 进入记录。每次实际检查，不借用另一脚本旧 PASS。 |
| 检查状态 | PASS / FAIL / NOT_EVALUATED / NOT_APPLICABLE / CHECK_ERROR 分开持久化和投影。外部文件 argv 没有声明内联正文时为 NOT_APPLICABLE，文件身份/授权保护另行执行；不读任意文件冒充静态覆盖。R 无已配置检查器，保留 NOT_EVALUATED、原非必需边界，不发明 PASS 或新执行禁令。 |
| 进展与准入 | 当前候选仍错、resolved、pending、真实失败与 unknown 并列。旧 `progress_proven` 是诊断，不能单独终止；Recovery、TypedGenerate、图路由和回执消费者一致。只有当前上下文中可证明的重复/循环、原总预算、正式停止、取消和实际硬故障具有停止权。 |
| 内部故障 | 必需语法检查未完成/内部异常走既有 INTERNAL_PROTOCOL 域。实际注入结果：1 次 Generate、0 执行、CHECK_ERROR，剩余 4，未额外请求模型纠正检查器。 |
| 报告 | 原子无覆盖 writer 保留。固定路径冲突与内部诊断、科学完成分别记录；优先已有显式诊断根，否则仅在当前已持久化 task/stop 对应的既有 task store 下保存内部终态 trace。没有开放任意写根、生成新的通用文件服务或改变固定路径义务。重复收尾复用可信 journal 与同一 stop 路径。 |

operation 保留正式 capability 的动作选择与冲突检查，未从 step_id 猜测或全局删除字段。当前公开规则已给出预期 operation；本单 G-A 故意保留缺失→补齐以验证部分修正。删掉的是默认重复填写来源映射的邀请，不是资源、效果、授权或后置条件。

## 连续图与真实副作用

固定 provider 只提供明确的业务响应；科学替身只在新隔离路径写合成内容。真实 handoff、Plan/审批、parser、helper、静态检查、repair ledger、执行器/新进程、attester、Reviewer parser、gate、Commit、重载、STOP 与 writer 都保留。未经准入候选不执行，测试没有预塞 approved/ready/succeeded。

| 图 | 最终候选结果 | 核心证据 |
|---|---|---|
| G-A success | 7 次实际执行，7 次唯一 Commit，全部步骤 passed | inspect → contig 两步先提交；A 缺 operation/code+argv/错误 binding_env；B 补 operation/argv-only/binding 仍错；C 省略映射、系统派生、正式审批执行，再完整走 prepare/build/annotation/summary/final。A/B 时 prepare 零执行，前两步不重做。 |
| G-A missing | 同样 7 执行/7 Commit | 在 B 的真实检查之后故意漏记该结果。持久记录和下一实际请求都显示 NOT_EVALUATED + unknown_targets；不会把漏记当 FAIL，也不会阻止原额度内 C 的合法修正。 |
| G-B stop | 2 执行/2 Commit，blocked | B 后 provider 正式返回 blocked；STOP 精确 prepare，当前 attempt 为空；build 与后继未开始。三固定报告受保护，三本次诊断由实际 writer 创建。 |
| G-C recovery | 3 执行/2 Commit，blocked | 格式错误→接纳→build 外层 rc0、内层255且缺必需效果→attester 不允许 Commit→Recovery 两次正式日志分页读取→新载体候选纠正/正式停止。STOP 为 build，当前候选 attempt 空、旧因果 attempt 独立保留；报告冲突和幂等收尾真实发生。 |
| G-D last | 第五份候选合法，7 执行/7 Commit | 中间两份装饰变化 `progress_proven=false`，仍受原剩余额度约束。最后已经获授权的合法机会可通过准入，额度不重置。 |
| G-D budget | 第五份仍非法，2 执行/2 Commit | 4 个额外纠正名额耗尽，错误码 GENERATE_REPAIR_BUDGET_EXHAUSTED。未执行 prepare，旧报告不动。 |
| G-D repeat | B 被原样再次提交，2 执行/2 Commit | 可证明同一非法候选复现后 GENERATE_IDENTICAL_CANDIDATE_EXHAUSTED。另有实际政策/账本 A-B-A、重载、去重、陈旧/取消保护。 |

预算单位仍为 additional_candidate_calls。A 拒收时授权 B 后余 3；B 返回时 **remaining_before_authorization=3**，再授权 C 后余 2。不能把授权前与预留后的余额混为一列。last/budget 的前四份拒收回执剩余依次 3、2、1、0；第五份不再产生新名额。已接纳候选退休旧比较依据但保留费用账本，Recovery 不刷新额度。

本单不是特判 argv-only：核心矩阵交叉 4 种载体与 6 种检查记录状态；同正文正/负语法、code/argv 冲突、helper/marker 作用域、binding 省略/显式正反例均验收。固定种子 **1789464978**，100 条、每条最多 20 个测试动作的有界政策序列，实际拒收序列在最多 5 份候选内结束；驱动真实 assessment/RecoveryPolicy，独立断言重复或原上限才终止。该小矩阵不是科学程序穷举，也不是 100 条全科学图；跨执行/Recovery/重载/取消/报告的不变式由上表正式图及真实 ledger/claim/late-result/writer 测试补齐。

## 交付、保护与发现的反例

终态固定输出保持 `required_output_status=conflict`、原 `delivery_status=failed`；独立内部 `internal_diagnostic_delivery.status=complete`，对应三份真实 write receipt；`scientific_completion=not_completed`。合成旧文件的字节、mtime 与 inode 不变。未知用户文件、同任务旧 stop、其他任务可信 journal 即使内容相同也不得借用身份；三个文件部分冲突、无权限写入、丢 receipt 恢复、真实双进程竞争、symlink、取消和迟到更新分别保留反例。

G-B 的 stage_status 明确两步 Commit、prepare 未执行、后继未开始；G-C 单独保存 build 的原因果执行，不混成 G-B 的事实。reproduce.sh 是诊断材料，本轮未执行它；没有运行原事故批准脚本。

本轮必须保留的开发发现：

- 原始字节 witness 重现旧误停；它不是 FIX PASS。
- 第一轮合成 Plan 缺 action/带多余字段，下一轮通用 capability 使用错 mode：都修 fixture，未放宽生产校验。
- 首版幂等测试传入原始边界字典，缺正式 status：改为调用现有 `terminal_boundary(values)`，未伪造终态。
- 漏记图先完成了实际 Commit，但下一请求缺 NOT_EVALUATED：发现真实公开反馈白名单遗漏，本轮补齐后最终图通过。
- 检查器故障 fixture 先缺 Plan 义务，修 fixture 后又暴露真实 INTERNAL_PROTOCOL 被按模型修复计费。本轮修到内部协议错误域，最终图证明不再多请求/计费。
- 原 `test_F20_primary_execution_flag_keeps_original_scope` 的 `steps=[]` fixture 在未改变的精确 step 保护处报 PREACTION_FAILURE_SCOPE_STALE；保留失败与原断言。其目标源文件与 BASE 完全同字节，未宣称全库测试通过。其他历史失败/未授权测试沿原主单保留，本轮不机械累加旧 PASS。

开发各次导出、失败及最终通过分别列在 `verification.json`；没有覆盖原失败账本。最终核心图和直接消费者在同一 final10 字节重跑，不用旧数量代替类别验收。真实 PTY/plain 保留同一冻结审批正文、F2/details、明确 Enter、EOF/空白粘贴/历史排队保护；已注册 schema2 hook 与 AskUser 正反例单列结果。

## 可复核材料与第 9 节回执

- `verification.json`：逐运行/逐图结果、预算、步骤状态、STOP 身份、报告 receipt、原失败和开发失败，以及完整提交/父版本。
- `trace_index.json`：实际 provider messages/候选、审批正文、执行脚本和 wrapper、stdout、Commit、状态/报告的定位及 SHA256；归档为 `evidence.tar.gz`，均来自新隔离测试。旧原事故仅保存只读 witness 摘要。
- `manifest_and_protection.json.gz`：最终导出字节、逐进程实际加载模块/关键 callable、受保护工作树和 index、净增量与 overlay；不含敏感配置内容或完整宿主环境。
- `commit_files.tsv`：完整 commit、父提交、文件范围。

```text
PUBLIC_DERIVED_FIELD_OWNERSHIP_RESULT: binding_env 默认省略，值单归 produced_bindings；显式错误仍拒
EFFECTIVE_CARRIER_CHECK_RECORD_PARITY_RESULT: 支持的明确内联载体共用实际操作数；外部文件未伪造语法覆盖
CHECK_UNKNOWN_FAIL_NOT_APPLICABLE_SEPARATION_RESULT: 持久记录与实际请求区分五态；漏记不当失败
PARTIAL_REPAIR_VS_ADMISSION_RESULT: 已修 operation 与仍错 binding 并列；非法候选零执行
HEURISTIC_STOP_AUTHORITY_REMOVED_OR_NARROWED: progress false 不再单独控制 Recovery/TypedGenerate/图路由停止
FINITE_BUDGET_REPEAT_CANCEL_LAST_CHANCE_RESULT: 原额外4次、真重复/ABA、重载/claim、取消/迟到、最后合法机会保留
REPAIR_LIFECYCLE_AND_ACTUAL_MESSAGES_RESULT: 已接纳参照退休、账本保留；三入口实际协议与反馈回归
EXACT_STEP_AND_STOP_IDENTITY_REGRESSION_RESULT: prepare/build 精确归属；当前未执行 attempt 与旧因果 attempt 分离
REPORT_OWNERSHIP_CONFLICT_IDEMPOTENCY_RESULT: 原文件不覆盖，可信当前 journal 幂等复用，真实冲突/权限/竞争反例
DIAGNOSTIC_DELIVERY_VS_REQUIRED_OUTPUT_STATUS: 内部诊断complete，固定交付conflict，科学not_completed 分开
REAL_MODEL_CALLS: NOT_RUN
REAL_SCIENTIFIC_CALLS: NOT_RUN
ORIGINAL_SESSION_RESUMED: NO
OLD_RESULTS_MODIFIED: NO
PUSH: NO
```

后续真实验证须独立授权：先固定一组合成小任务、调用上限和新输出根验证真实 Generate 的省略与纠正；再指定实际可用工具和小科学任务验证后置条件；最后才是正式任务/新输出位置。每阶段记录全部尝试和分母，不能跨 session 挑最好一次。当前固定 provider 的机制 FIX PASS 不证明真实模型稳定性或真实 snpEff/科学 E2E；原 V2 未独立提交 overlay 的边界仍存在，未扩大授权、安装或 push。
