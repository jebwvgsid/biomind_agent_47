原始业务字节与已准入状态分开保存。

- `before_plan.json`：本轮独立旧 F6 normal 的真实已准入持久快照；原 provider response 未被旧 driver 保留，只有真实 `raw_digest`。不能把此文件称为原始 response。
- `after_plan.json`：本轮 renamed_repeated 的真实 provider Plan UTF-8 原文，由 `/raw_plans/0` 提取且与生产记录的 digest 相等。
- 两份 `*_task_spec.json`：双方真实 TaskSpec 完整投影。三个输入、五个 required 用户输出的公开履约字段相同；具体临时根和权威身份不同。后者保留原目标全文，并明确允许重排、加强实际检查要求。
- `generic_split_actual_plan.json` 是另一个明确新增 preflight_log 义务的例子，不能用它单独声称同等任务输入与输出比较。

来源、JSON 路径与哈希见 `source_index.json`；语义逐项与重复字段计数见 `../same_obligation_comparison.json`。
