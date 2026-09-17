# 非空 required 的真实执行闭环

`tests/test_unified_runtime_graph.py` 在隔离新进程中 **2 passed**。此组使用当前有效 Planner 协议，保留真实 graph、持久化、runtime 证据接纳、binding、代码批准、prelaunch 检查、runner、结果解析与 Commit；仅外部模型回答及只读 inspector 的 process_factory 进程叶子使用合成输入。真正执行的只是临时目录里的微型 shell observation fixture，不调用科学工具，不安装软件。

首发情形从真实 Consultant、Planner、PlanReview 开始。非空 `runtime.tool.bin` required 在调用 Generate provider 前产生 `runtime_evidence_required`，通过真实 inspector adapter 构造的只读、断网沙箱命令与真实 Recovery 接纳证据后，回到同一步 Generate：一次 provider、一次批准、一次真实微型脚本执行、一次正式 Commit。重载的正式 runtime registry 包含该 binding。进程叶子被替代，因此不把这个结果称为服务器 bwrap 实际可用。

修订情形先通过真实 Plan/provenance API 记录已批准旧契约、空 runtime manifest 的旧动作，以及已结束 FAILED attempt；这些是合成历史事实，并非重跑用户失败会话。随后通过 `carry_forward_plan_lifecycle` 与真实批准 API 创建 required 非空的新 Plan revision，使用真实 typed runtime graph 执行后半链。步骤与 TaskSpec revision 由正式 `bind_plan_to_task_revision` 绑定，未经绕过 freshness 检查。

该情形验证：旧 action 整个 record 保持相等，旧 manifest 仍为空，旧 attempt 保持 FAILED；正式新 revision 产生且仅产生一次 `authority=plan_revision` 的 scope preparation，execution epoch 加一，重复 preparation 不再递增；之后发现、Generate、批准、真实微型执行、Commit 各一次。旧批准及新 Plan 版本记录在证据中。新 action 受当前 runtime manifest/批准 hash 约束，不回填旧 action 的 runtime 字段。

逐项结果、受测源码 hash 和两条实际请求/批准/执行/Commit 回执分别见 `evidence/runtime_graph_results.json`、`runtime_graph_pytest_results.json`、`unified-required-runtime-graph.json` 与 `unified-required-runtime-graph-revision.json`。局部通过数不作为全量累计回归总数；最终以主任务冻结候选后的统一运行结果为准。
