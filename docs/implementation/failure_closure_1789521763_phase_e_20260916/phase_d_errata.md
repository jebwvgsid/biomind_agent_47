# D 历史勘误（追加，不改历史）

依据原审计提交 `332d91b6307fde96a6f64b434722fe4067cdc164`。原 D 目录、回执和 74 行表保持原字节。

| D 行 | 问题 | 正确解释与 E 证据 |
|---|---|---|
| D1-02 | 未解析 scalar 冻结成功的测试 PASS，同时分类为“已修/保护有效” | PASS 指缺陷见证成立。E 才验证具体字段准入拒收。 |
| D1-10 | 坏 scalar 到达真实审批，被混列已有修复 | D 在审批中断处停止，未执行坏候选，不能推定后续成功/失败。E 的坏候选不进审批，局部纠正后完整 Commit。 |
| D1-12 | `NOT_IMPLEMENTED_STAGE_E` 与“当前已修”分类冲突 | 当时尚未实施。E 新测试完成真实批准/执行/核验/Commit，历史字段不回写。 |
| D2-03 | malformed effect 见证可能仍走现存结构化回执/inspection | E 真实注入不可解析 stdout，结构化 effect receipt 不存在/不可用，再检查 Execute 及首 Recovery 请求。 |
| D2-14 | 既有 CAS 代码被描述为首请求已覆盖，但新注入 wire 尚 UNKNOWN | E 在 Commit 正式保存处抛 CAS conflict，首请求保留期待/current generation。 |
| D2-18 | AskUser 被泛列 recovery_pending 来源 | 当前 TypedAskUser 只输出 planning_ready/consultation_ready；实际 recovery_pending 重入在 Triage，E 覆盖该正式入口。 |

分类拆为四列：测试执行、见证性质、修复/保留状态、证据级别。[E 对照表](boundary_coverage_74.tsv)逐行保留原 D status/classification，再列 E 结论。历史聚合、没有可达入口和外部服务范围分别保留 STATIC_ONLY、NOT_APPLICABLE、NOT_RUN，未统一改写成 PASS。

补充夹具勘误：在真实审批等待期间仅更改 durable draft，不等于替换 checkpoint 所有的冻结动作。旧 `y` 能批准并执行原字节，但没有授权草稿改动。E 验证执行 hash 与旧冻结 hash 一致，工作区仍使用原批准 binding；新 draft 执行数为 0。此前将“原动作也必须零执行”作为断言的失败保留在账本中。
