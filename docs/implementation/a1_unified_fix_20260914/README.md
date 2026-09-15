# BioMind A1 统一实施与隔离验收

**IMPLEMENTATION STATUS: COMPLETE（限本轮授权的 F1—F6 框架机制）。事故链 FIX PASS；必选组合机制 FIX PASS。** 生产修复已提交，真实模型/API 和真实科学 E2E 均为 NOT_AUTHORIZED / NOT_RUN，原 session 未恢复，未 push。

实施 BASE：`b877b28b2b0366c04e9042bad09e1ee26a138ab0`（tree `d28fe5f7f771166ae11555532e3b1969c696fb78`）＋原保护 overlay。最终生产候选：`dbaa3f8e11bc56524ba2477c986d722657bf2b0c`（tree `c02d4b431d5d85ec7120cea621ac49c76812a026`）＋登记 overlay。[最终导出 manifest](final/final_export_manifest.json)为 457 文件，signature `332a06e803cba6fef3e54d2648fef1f05703c1f8b3656637725a6a674934c5f9`。逐进程实际加载模块另有回执；提交父版本、测试加载字节及复用证据分别登记；不把 disk HEAD 或目录名当加载证明。

[A1 审计](../../audits/execution_architecture_20260914_unified_a1/README.md)及其 `d146a479c437f190a19041d7ee0a7251aaeb9f4d`＋overlay 基线保持原样；历史 witness 不计入本轮 FIX PASS，未重启全节点审计。

| 切片 | 实际结果与关闭证据 |
|---|---|
| F1 | 普通、Recovery、局部纠正共用当前 mode 投影；完整动作/停止例子经正式 parser 接纳，动作 JSON 与 stdout 事实分开。独立示例实际计算 False/空列表，未给当前步骤造事实。[6项协议回执](f1_protocol/unified_final/current_final08_evidence.json) |
| F2 | helper 在审批前进入实际解释器/argv 载体，code/argv 保持一致。正常及恢复六步图使用 native argv-only；另有 native code-only、legacy Python/Bash 正式审批执行。字符串内 marker、非 stdin heredoc 等负例受控拒收。[作用域](f2_scope.md)、[正式图](f6/README.md) |
| F3 | 坏 env 保留 parser 首因；真实检查点统一上下层进展判断。重排、无关新字段和旧账本新增诊断不能买进展，真实字段修复/移除和最后合法机会可继续；旧预算、去重、重载、主动停止及取消保留。33项＋Recovery独立重载1项；真重复/ABA另走完整 END/报告。[env before/after](f3_env_progress.md)、[F3](f3.md)、[claim](f3_recovery_claim.md) |
| F4 | 回调直接抛错的 harness_error 正确 END；缺答复、question_missing 与正常匹配保留。6新例＋43旧输入审批保护通过。[最终49项](final/final08_root_verification.json) |
| F5 | compact 默认展示同一冻结 Plan body；F2/details 同对象，Enter/current owner/receipt/CAS 不退化。14真实PTY/plain通过，另补旧display24＋model-progress1（四模式）。[终端证据](entry/unified_final/README.md) |
| F6 | 22场正式图组合、4场真实终止摘要异常/取消、14场已注册hook正常/异常通过；覆盖 IT-01—12。[矩阵](acceptance_matrix.tsv)、[完整图回执](f6/verification.json)、[根回归](final/final08_root_verification.json) |

正常图从正式 handoff、六步 Plan 和审批开始，真实串联 check contigs → prepare → build → annotation → summary → final deliverables。prepare/build 在同一 mutable identity 上两次实际推进、attestation 和 Commit；annotation 消费这条链得到的 ready 版本。正常图 6 次步骤执行、6 次唯一 Commit，产出 annotated VCF、summary、report.md、reproduce.sh、stage_status.tsv 五项物理交付。

恢复图从本轮新小进程 rc7 开始，真实 Recovery retry_step、多轮 JSON/外壳/env 拒收反馈进入下一请求，然后新审批、执行、证据和 Commit；10次 Generate、7个实际 attempt（首次7，其余0）、6次 Commit。真重复/重排/无关变化停止时余3，A-B-A停止时余2；这些图均无代码审批、Execute 或 Commit，并经真实终止 owner 写三份状态报告。另有3条[来源字段正反图](f3_stop_origin.md)：Generate来源正确、合法blocked类型正确、真实Execute母因的attempt/rc保留。既有正常 Plan 审批已从正式冻结入口取得，未预塞 ready/approved/succeeded。

固定回复下移到外部模型叶子；主图保留真实 `_ainvoke_node_llm`/`_ainvoke_llm`、调用登记/消费、传输记录、parser、helper、审批、执行器与新进程、attester、Reviewer parser、gate、Commit/CAS、重载和 report writer。科学替身仅写新合成资源。外部运行能力观测值也显式替代，但其正式 evidence identity/持久化/解析真实。组件测试各自的替身边界单列，未将较窄组件结果冒充完整传输或真实模型验证。

[总账本](verification.json)、[每个缺陷→代码/正反验收](changes_and_acceptance.tsv)、[实际请求/审批/执行/Commit/report](f6/compact_evidence.json)和逐进程加载清单共同构成证据。343个非测试源码/公开资源不等于全部文件均被执行，也不证明所有Skill科学可用。F5及不受影响旧CLI结果按[精确依赖和调用边界](entry/unified_final/candidate08_display_reuse.json)复用，明确保留其真实加载版本；其余受影响公共调用方已在最终候选回归。

生产逻辑提交（完整文件范围、父提交、tree另见[提交账本](final/commit_files.tsv)）：

| 完整 commit | 文件范围 |
|---|---|
| `499ce1ae5b95a04dd227e21628578946203372f6` | `agent_core/step_effects.py`, `bio_agents.py`, `prompts/manager.py`, `tests/a1_unified_fix/export_and_run.py`, `tests/a1_unified_fix/test_f1_f2.py` |
| `a84e9932d66019a2277bf1ffb8b13ac7a5e39af6` | `agent_core/workflow/runtime_graph.py`, `tests/a1_unified_fix/test_f4_ask_user.py` |
| `2966f5ef3d0889201101192e397234e9b752cec9` | `agent_core/cli_plain.py`, `agent_core/cli_review.py`, `agent_core/cli_terminal.py`, `bio_agents.py`, `tests/a1_unified_fix/test_f5_pty.py` |
| `267d3b746a94c0db0f43509cea4e77cba3262577` | `agent_core/recovery.py`, `agent_core/workflow/candidate_repair.py`, `bio_agents.py`, `tests/a1_unified_fix/test_f3_claims.py`, `tests/a1_unified_fix/test_f3_late_result.py`, `tests/a1_unified_fix/test_f3_progress.py` |
| `8bf302c37bb38259d4878524ddc6e2d0e2a27e60` | `agent_core/step_effects.py`, `bio_agents.py`, `prompts/manager.py`, `tests/a1_unified_fix/test_f1_f2.py`, `tests/a1_unified_fix/test_f1_protocol.py` |
| `a64ca7652c74f505242620535788524d3f365241` | `agent_core/step_effects.py`, `tests/a1_unified_fix/test_f2_scope.py` |
| `e9df3ece9fceae24b782f5f1eb682926f8ed9f11` | `agent_core/workflow/candidate_repair.py`, `bio_agents.py`, `tests/a1_unified_fix/test_f3_env_progress.py` |
| `2b989a5eb6bd6b203805a1fbff216e8fb1a27cf7` | `agent_core/workflow/candidate_repair.py`, `tests/a1_unified_fix/test_f3_env_progress.py` |
| `dbaa3f8e11bc56524ba2477c986d722657bf2b0c` | `agent_core/workflow/managed_stop.py`, `tests/a1_unified_fix/test_f3_stop_origin.py` |

最终验收产物提交仅包含本轮拥有的 `tests/a1_unified_fix/` 和本目录文件；完整 hash 随交付回执返回。生产提交经独立 index 与 HEAD CAS，只同步本轮条目。[保护记录](final/protection.json)确认56个其他安全 dirty 文件原字节、相交 prompt 与 managed_stop 原 delta、其他 index 条目及旧 A1 均保留；没有收编原 HASH_OUTPUT_POLICY hunk。五个语义 overlay 独立登记，retriever 的 CRLF 原字节差异另列。敏感配置只检查 Git 元数据。**没有验证裸 commit/干净克隆独立部署通过。**

已有失败保持可见：旧9项缺 candidate scope 的 fixture 失败未删；最终公共39项为38 PASS、1项 BASE 已有的整字典形状断言失败（另2个subtest PASS），旧断言未改。相关类和断言的基线等同性见[公共回归](regression/final08/verification.json)；未授权旧事故/其他benchmark输入的测试逐项 NOT_RUN_SAFETY，见[preflight](regression/preflight.json)。开发中的旧失败、外层watchdog124、进程-11、测试输入时序和反馈历史假设均保留原回执，不计成功；-11原因未证实。

[独立限制](remaining_limits.md)：真实模型/API与科学工具未运行；原任务完整历史加载 UNKNOWN；R、external_worker、宿主登录环境及全部OS/断电窗口未验证。Recovery未知远端结果沿既有 at-least-once 协议记账，不宣称远端 exactly-once；已完成缓存的真实Generate重载为零provider调用。原session NOT_RESUMED，原批准脚本 NOT_EXECUTED，未安装、未读正式科学输入/业务库，PUSH NOT_PERFORMED。机制通过不代表原 snpEff业务或真实科学目标已完成。

## 2026-09-15 补充：session_1789437662 真实 Plan 与 F6 差异

[补充验收入口](supplement_session_1789437662/README.md)对照本次三份真实 Plan、第四 STOP、完整反馈/下一请求及最终候选 `1d0ab2cc6bd8d0c9e7ee2990725f8d41cd92c9c2` 的 F6 字节。本节限定上述原结论的适用范围，不覆盖旧回执，也不启动第二次全架构审计。

现场第三候选把 prepare capability 移到 `runtime_healthcheck`，角色关联仍指向不存在的 `prepare_database`。当前 scope gate 的首个失败条件是查表为空后的 capability 不匹配。仅修这一字段、保留其余候选和全部任务义务，新正式 handoff/完整准入可到真实 PlanReview `approval_pending`。第四原始响应选择正式主动停止；合法第四候选仍被允许，预算未扩大。原 session 无 Plan 批准、执行或 Commit，停止报告发布不等于科学目标完成。

原 F6 固定 provider 人工预填同名步骤/operation 与同步引用，没有经历真实 Planner 纠正；旧 P_test 的输出声明与默认结果模式也不同。原下游固定替身机制结论继续按其范围成立，未证明真实模型能从请求形成或纠正 Plan。本次完整请求已有单字段修复所需核心规则，但公开投影删 `step_id`、READ_MISSING 文案混淆输入与关联、SCOPE_INVALID 不区分查表未命中；另有 `skill_ids` 示例身份占位符错误（非现场触发因素）。

以下精确项已并入 [changes_and_acceptance.tsv](changes_and_acceptance.tsv) 的 `LIVE-PLAN-*` 行，**本轮只读复核，生产修复均 NOT_IMPLEMENTED**。依赖顺序是现有诊断与公开投影 → 身份示例 → 复用 F6 接真实候选变体 → 另行授权的有限真实 Planner-only 验收。保持角色作用域校验、原四次预算、主动停止/取消、审批正文/输入及已完成 F1—F6；不增加全局 validator、Controller 或 binder。

补充验证将原始失败 witness、当前完整准入正反例、旧断言失败见证和原 F6 下游机制结果分别登记。实际加载、复用依赖、开发失败、未运行项和本轮提交范围见 [补充账本](supplement_session_1789437662/verification.json)。本轮没有生产修改、新真实 API/科学执行、原 session 恢复或 push；待审批不等于改名候选的下游六操作已通过。


## Plan 规划权 V2 实施与补充验收（2026-09-15）

本轮 V2 已实际修改 Plan 准入、同源公开 schema/prompt 和直接消费者，替代此前 LIVE-PLAN 中原准入集合与重复布局冻结的限制。原 F1—F6、旧事故与失败账本保持原范围。入口：[Plan 规划权 V2 README](plan_authority_v2_20260915/README.md)；[第 10 节完整回执](plan_authority_v2_20260915/implementation_report.md)；[机器验证账本](plan_authority_v2_20260915/verification.json)。

已增加显式通用 Bash/Python 执行，v3 resource_uses 单来源和 TaskSpec 输出义务读取，降级无硬依据的三 Skill/READ 文字解释偏好，并同步当前公开动作/停止示例。保留未知对象、来源与依赖、权限、批准对象、证据、必需输出、预算/去重/停止/取消和输入保护。新 8 个完整图场景、旧 22 个图场景、额外公开协议与保护回归逐场列版本；244 个唯一 pytest ID 为 243 PASS + 1 已存在的 WGS 版本断言 FAIL，另有真实 PTY/plain 14 场景 PASS。固定 provider/科学替身不构成真实模型或科学 E2E。

整体状态 **PARTIAL**：10 个生产文件及 18 个测试文件的可隔离净补丁已提交；公开投影的另一处 V2 增量与先存未提交 planner_context 基础函数相交，保留工作树改动及精确 patch，未收编旧函数。已测候选是安全工作树 + 保护 overlay，不等于裸提交。最终 current14 468 文件签名 `5b99904ba6d63cf4486234cddaeb2b339a959b2a562cb40cc6352202ef781212`；逐进程实际加载证明另存，导出清单不冒充加载证明。生产逻辑最后提交 `5673b236c80a694e27e72e7b0f90f0a9946bf4b9`，完整链与净文件范围见分册 commit_files.tsv / net_change_summary.json。未恢复原 session、未调用真实 API/科学工具、未 push。
