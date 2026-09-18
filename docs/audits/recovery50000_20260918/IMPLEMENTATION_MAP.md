# 现有入口 → 缺陷 → 修改位置 → 验收

这是一份实施索引，不替代原任务书或另立审计计划。完整精确文件集合以 `COMMITS.json` 为准。

| 切片 | 现有入口与已证实缺陷 | 本轮修改位置 | 验收重点 |
|---|---|---|---|
| P1 | Recovery reserve/adapter 仍继承 8,192；SDK 改字段并丢原生消息内容 | config/config.py；bio_agents.py；workflow/context_compaction.py、recovery_runtime.py；deepseek_stream.py、scoped_context.py | 最终 SDK max_tokens=50000；较小已知 cap 明示；完整计量；其他 provider/节点额度不误改 |
| P1 | length 直接停止；半回复与持久重入不能刷新配额或重放动作 | workflow/recovery_agent.py、recovery_node.py、recovery_context.py；context_lineage.py | 最多两次额外完整重交；缺终态零执行；工具/压缩/跨进程计数；日志损坏拒收；已接纳回执重入 |
| P2 | artifact 修正投影把非产物 issue 改写成路径错误 | controller_audit.py | 未知码保真；真实 Generate 下一次 payload 保留 helper 包装原因；修正 helper 真执行 |
| P3 | GWAS Skill 布局诱导观察步骤凭空声明目录；Gate 额外要求第二份成功回执 | 两个 GWAS SKILL.md；completion_gate.py、semantic_completion.py | 观察 Commit 无目录；冻结错误目录仍拒收；正式修订/审批；假 passed、错身份及错内容拒收 |
| P4 | 整行 ASCII 解码混淆 UR 与科学内容；发布后临时 URI 悬空 | genomics_reference_bundle.py；semantic_completion.py | 字段字节诊断；v1 窄兼容/v2 最终 URI；中文与转义；SN/LN/M5、FAI、BWA 实质检查 |
| P5 | 重复手写脚本导致参数错误、子命令失败漏传、重跑冲突 | wgs-short-read-mapping SKILL.md、references、scripts/prepare_reference.py；bio_agents.py attempt 环境变量 | 真实四工具链；原始 rc；坏组件重建；归属保护；no-replace 竞争；发布中断重验零重建再 Commit |
| P6 | 指定报告路径冲突到结束才发现；终态丢失已有核验根因 | bio_agents.py Consultant commit；workflow/runtime_recovery_reporting.py、recovery_projection.py、inspection_stop.py、managed_stop.py；cli_lifecycle.py | 规划前只读预检与用户决策；最终竞争保护；当前原故障/Recovery 停止/Commit/交付分列 |

测试替身只承担外部模型返回、故障注入或显式审批/Reviewer 选择。未用手工 binding 证明自动环境发现；原自动发现回归使用生产候选发现、受控检查、请求更新、审批和 runner 消费链。原 bwrap 失败分类继续保留尚未验证，不转为软件缺失或安装触发。
