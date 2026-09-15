# Plan 规划权 V2：实施与验收入口

本轮实际修改 Plan 准入、公开协议及直接消费者。原 A1 / F1—F6 的限定机制结论、历史现场与失败账本保留；V2 替代旧 LIVE-PLAN 中冻结原准入集合和重复填写布局的要求。未新增 Controller、独立纠错 Agent、平行 Plan 或语义猜配。

当前实际测试对象是安全工作树加受保护 overlay。BASE `81d10c58a236f44c7987f1913190e6927ce23248`；最终候选导出 `current14` 的 468 个文件签名为 `5b99904ba6d63cf4486234cddaeb2b339a959b2a562cb40cc6352202ef781212`。文件清单不是每个文件均已加载或执行的证明；逐进程加载另列。

最终状态、提交和唯一测试计数见 [verification.json](verification.json)，任务书第 10 节逐项回执见 [implementation_report.md](implementation_report.md)。本文件是原主单的本轮分册，不另建总任务。

## 实际变化

- 增加明确的 `framework.generic.execute`，复用原 Bash/Python 生成、代码审批与执行器。领域 Skill 不再是所有执行步骤的必需前提；未知 capability 或工具仍拒绝，不能静默回退。
- 新公开契约 `metadata.resource_contract_version=3` 由 `step.resource_uses` 一次声明资源身份、read/mutate 和需要时的 helper role。旧 `inputs` 和资源侧 `role_associations` 不再要求模型双写；内部只读投影按精确 ID 生成。输出生产、状态转移和真实证据是不同事实，继续保留。
- TaskSpec 的 required、路径策略与固定路径按精确输出身份读取并随审批冻结。模型只补尚未确定的相对路径；不能覆写或降级正式义务。未采用旧 workflow 时，不再强制填写空的 batch_id/workflow_stage_ids。
- 每步三 Skill 的偏好和删除失效 READ 后必须补叙述理由的要求降为建议。建议不单独产生拒收、修正请求、无进展指纹或新的确认门槛；选中 Skill 的真实性与范围仍校验。
- 公开动作/停止示例按真实 mode、通用工具和已公开资源生成；最终 5 项定向验证原样回传实际请求中的示例，旧错误与遗漏断言保留。
- 补通真实纠正回执到下一模型请求的公开字段；泛化执行语言与已批准工具不一致时走原格式/语义纠正账本。完成门对 mutable workspace 使用已核验的状态链及精确目录，继续要求必要不可变产物的有效版本。

## 证据导航

| 内容 | 入口 |
|---|---|
| 规则位置、约束来源、处置、消费者与正反例 | [resource_rule_disposition.tsv](resource_rule_disposition.tsv)、[public/rule_disposition.tsv](public/rule_disposition.tsv) |
| 单来源与审批、重载、完成门直接复核 | [resource_contract.md](resource_contract.md)、[resource_direct_review.json](resource_direct_review.json) |
| 实际公开请求、反馈、首轮停止、第四机会、空 Skill 完整图 | [public/README.md](public/README.md)、public/runs/current07 |
| 通用只读/本地写入、异名重复操作、拆分/合并、恢复/受阻与实际下游 | [graph/README.md](graph/README.md)、graph/runs/current07 |
| 受影响旧 F6 22 场景、保护与真实 PTY/plain | regression_runs、protection_runs/final_unique_verification.json、public/pty/current07 |
| 安全 BASE、原 index 与原工作树 | [baseline/start.json](baseline/start.json)、[baseline/source_manifest.json](baseline/source_manifest.json) |
| 历次候选字节和失败（不覆盖、不计为最终成功） | development_evidence、development_runs.json、各分册 development/runs |
| 净提交与保护 overlay 的精确分离 | [protection/net_isolation_current08.json](protection/net_isolation_current08.json)、[protection/intersecting_hunk.json](protection/intersecting_hunk.json) |

测试固定外部 provider 回复、科学叶子及必要外部能力观测，使用新任务、合成文件和真实小进程。实际 parser、审批、批准载体、执行器、attester、Reviewer parser、gate、Commit、重载和报告按每场景记录。应用 messages 不是线上 wire 原包。不存在真实模型可靠性或真实科学 E2E 结论。

“通用只读”是正式声明及测试动作的行为，不能解释为已验证任意代码的 OS 系统调用隔离。本轮沿现有安全导出/guard；未扩大宿主访问或增加 OS 沙箱。测试看门狗可显式为 360 秒，生产调用预算和主动停止规则不变。历史 180 秒超时保留为未通过。

Git 的一处独立限制：`planner_context.py` 的公开投影基础函数在 BASE 已是他人的未提交 overlay，V2 精确改动与其内部相交，无法单独形成不收编旧函数的提交。工作树中的授权改动和精确 patch 保留，其他可分离逻辑提交。不可把裸提交称为本轮测试通过的候选；详情及状态见最终回执。
