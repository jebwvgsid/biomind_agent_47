# Generate 停止报告的首因来源

最终候选 **08 的三条正式隔离图 3 PASS，实际 rc=0**；457 个导出文件前后 hash 一致。局部修后首次运行的 **3 PASS、50.21 秒** 单独保留。原候选 06 的字段错误、修前失败和当前原始回执分别保存在 [verification.json](f3_stop_origin/verification.json)，没有将旧 F6 的 22 PASS 冒称该字段已通过。

原真实重复候选图中，`stage=generate`、首因 `ACTION_SCHEMA_TYPE_ERROR`、primary owner 为正式 `GENERATED_ACTION`，Recovery/Reviewer 调用均为 0，执行和 Commit 均为空；但报告 `origin.source_stage=recovery`、`source_owner=semantic_review`。原 F6 测试检查首因错误码与无 attempt，没有检查来源字段。

该字段表达原始首因，而非停止决策 owner：`inspection_stop.build_inspection_stop` 沿明确的 causal parent 保留原始故障；`managed_stop.explanation` 将“来源阶段、原始错误、停止阶段”分别显示。直接首偏差是 `build_managed_stop` 对无 packet 的 Generate 构造通用 seed，未传 source/owner，helper 默认写入 recovery/semantic_review；后续 `setdefault` 无法替换该默认值。严格 `GenerateOutcome` 根对象也没有可推断节点的来源字段。

实现仅在 `managed_stop.build_managed_stop` 既有 `not packet` 分支插入五行：当前边界确为 Generate 时，将来源写为 generate；owner 使用已有 primary owner/error_owner，无证据时保留合法 UNKNOWN；正式 blocked 的报告类型写为 blocked。有真实母 packet 时完全沿既有来源投影，`inspection_stop.py` 未改。原保护 overlay 行未修改，root 单独抽取本次插入 hunk 提交。

新增 `test_f3_stop_origin.py` 验证：

| 路径 | 真实证据 |
| --- | --- |
| Generate 真重复停止 | 正式六步 Plan/审批、真实 transport/parser/ledger/终态持久化和三报告；来源 generate/GENERATED_ACTION、无 attempt、无执行；修前同边界断言失败 |
| 合法主动 blocked | 第一真实步骤已有 Commit，后续模型正式 blocked；来源 generate、owner UNKNOWN、类型 blocked，没有虚构当前失败执行 |
| 真实 Execute 母因 | 新建批准脚本实际 rc=7，随后 Recovery 事件持久化故障；停止阶段 recovery，原来源仍 execute，原 attempt/rc 保留 |

同新任务状态还直接调用纯报告构造函数验证缺 owner 时为 UNKNOWN；这项调用不写状态、不创建执行资格。三图使用固定外部 provider/科学能力叶子，真实审批、执行、证据、报告装配与持久化仍走生产代码。原 session、原脚本和真实外部能力未使用。当前指针为候选 08；原 06 压缩物证另存本分册，避免历史证据链接随最新结果目录更新。
