# BioMind 远程工作台：前端接入定点审计

审计日期：2026-09-14。范围仅限可供远程工作台接入的入口及其权威边界。没有重跑全仓历史故障审计，没有运行或导入 BioMind。

## 工作区与授权依据

- 操作系统：Linux，主机 `ln02`，x86_64，内核 `5.14.0-284.11.1.el9_2.x86_64`；不是 Windows。
- 起始目录：`/public/home/liusiqi/03.毕设`；实际仓库：`/public/home/liusiqi/03.毕设/Biomind_v1`。
- 分支：`refactor/consultant-controller-authority`。
- BASE HEAD：`d1f3779407e891d6fadf1778cd8e0ce7804b1630`。审计针对该 HEAD **加当前已有工作树修改**，不是只审计提交快照。所引用代码的 SHA-256 见 `source-evidence.json`。
- 起始工作树有 69 项 tracked 修改/删除，另有既有 untracked 文件；起始索引树与 HEAD 一致，没有把既有改动暂存。保留原分支，新增隔离目录 `apps/remote-workbench` 和本报告目录；不 stash/reset/clean，不 push。
- 适用范围内未发现 `AGENTS.md`。读取了项目开发记录 `docs/implementation/cli_v2_20260912/README.md` 与 `docs/implementation/cli_terminal_20260911/report.md`；沿用显示层不得取得执行权威的约束。

用户直接委托是本轮授权范围。附件《BioMind_Windows远程工作台_架构建议与Codex首轮任务书_20260914.md》作为补充任务书读取，重点落实第 7 节，不把附件里的建议自动视为已批准的生产实施。附件 SHA-256：`1a16606fe48468664ae6d98ae3420345f8522d3fe6a4779fc49e82654a58e044`。

本轮没有读取、输出或执行用户禁止的启动脚本；没有读取凭据、私钥或完整环境；没有运行旧会话、生信分析或真实模型；没有终止既有进程。浏览器检查中只结束了本轮创建的临时测试进程。未修改生产 Python/TypeScript 运行时。

## 能力分类与源码依据

下表“已有”指源码中可核验的内部能力，不等于存在可调用的远程 API。“薄适配”仅转换传输、输入和展示，不重新决定计划、执行或结果。

| 接入需求 | 结论 | 当前源码证据 | 工作台接入含义 |
| --- | --- | --- | --- |
| 启动与用户输入 | 内部已有，需薄适配 | `bio_agents.py:54594` `_runtime_main` 创建 `BioAnalystAgent`；`:54621` `main` 经 `agent_core.cli_terminal.run_terminal`；`:54065` `interactive_mode` | 现入口依赖 TTY；不能用模拟键盘、拼接 CLI 命令或启动另一个 Codex 作为服务。下一阶段新增独立服务启动入口，接入同一 BioMind agent。 |
| 新建会话/提交 | 内部已有，需拆出轻量会话记录 | `bio_agents.py:53578` `go` 绑定 `thread_id=task_id`、持久化提交并执行；`memory/persistence.py:69` `PersistentStateManager.create_task` | `go` 不是纯创建接口。GUI 空会话只建元数据；明确发送才进入运行。消息须保持原始 Unicode、多行及文件引用。 |
| 历史会话与详情 | 已有存储，需薄适配 | `memory/persistence.py:136` `get_task_context`、`:155` `list_tasks`；`memory/session_archive.py:331` `recent_events`、`:347` `events_for_task`、`:356` `resume_events` | 按工作区/用户过滤并分页，投影公开字段；构造器与某些加载器会初始化/修复存储，不能从页面渲染路径直接调用。 |
| 持久状态与恢复 | 部分已有 | `bio_agents.py:2874` 使用 `MemorySaver`；`:5712` `_build_schema2_resume_runner_state`；`agent_core/plan_tracker.py:10657` `load_plan_state`、`:10847` `save_plan_state`；`agent_core/human_io.py:470` `save_waiting_state`、`:498` `load_waiting_state` | 有 PlanState、历史与 waiting state，但不能据此宣称 LangGraph 内存检查点跨进程持久。进程退出后的业务恢复须由既有 runtime 判断并显式操作。 |
| 正在运行时重新附着 | **缺失远程只读接口** | `bio_agents.py:54361` `/resume` 分支进入 `_run_agent_interrupt_loop(Command(resume=...))` 或 `resume_initial=True`；`:53814` `_run_agent_interrupt_loop` 会调度执行 | 不能把 `/resume` 当 reconnect。需要向仍然存活的执行所有者读取快照、订阅事件。连接重建不得再次调用 `go`。 |
| 执行所有权 | 有归属记录；**缺少已证实的全程互斥** | `execution/profiling.py:659` `RunProfiler`；`:685` `_process_identity`、`:694` `_lease_owner_alive`、`:705` `reconcile_stale_run`、`:741` `start_task`、`:838` `heartbeat` | lease 包含 run_uuid/PID/进程身份/心跳，可识别遗留状态。`start_task` 在本进程 `_tasks` 无记录时仍可新建 lease，未发现它对另一活进程持有者作排他拒绝。PlanState CAS/文件写锁不等于运行互斥。下一阶段必须用共享所有权门禁防止 CLI 和 GUI 双执行。 |
| 计划审批 | 权威校验已有，输入需薄适配 | `bio_agents.py:40398` 局部节点 `plan_review`，`:40777` 起生成身份与完整审阅内容，`:40828` 起重新检查 submission；`agent_core/plan_tracker.py:8488` `build_approval_projection`、`:8539` `build_approval_scope`、`:8582` `approve_plan_state`、`:8796` `plan_approval_is_current`、`:8822` `validate_action_approval_lineage` | 由原节点重新校验当前提交、版本和 lineage 后批准。waiting payload 中内容截取 2000 字，不能直接当完整审批卡；须保留完整、经过显示清理的权威审阅投影。 |
| 脚本执行审批 | 权威校验已有，输入需薄适配 | `bio_agents.py:46081` `execute_logic_impl`，`:48010` 附近脚本检查、provenance、`middleware.review_code`；`agent_core/middleware.py:1186` `HITLMiddleware.review_code`、`:1277` `_parse_code_response`；`agent_core/action_runtime.py:946` `build_action_approval_identity`、`:968` `validate_action_approval_identity` | 与计划审批独立。身份涵盖脚本 SHA、capability、runtime/artifact 环境、执行策略及工作目录；前端只回传不透明身份，不只传 `approved=true`，不自行计算“已批准”。 |
| 公开消息、工具与模型活动 | 观察点已有，需薄适配；可靠远程事件流缺失 | `agent_core/cli_display.py:19` `safe_text`、`:31` `safe_document`、`:86` `emit_public`、`:152` `bind_terminal`、`:158` `emit`、`:184` `DisplayModel`；`agent_core/provider_attempt.py:999` `_apply_observer`、`:1041` `_emit`；`agent_core/cli_activity.py` tool lifecycle decorator | 目前显示绑定是全局 terminal sink；序列和显示队列不等于持久多客户端订阅。不得抓取 ANSI/TTY 文本反推状态，不向客户端暴露思维链或完整 provider payload。只有已有公开文本才显示；无 token delta 时显示运行阶段与等待时长。 |
| stdout/stderr 和运行日志 | 内部已有，访问需薄适配 | `agent_core/cli_display.py:413` `StreamDisplayCopy`；`execution/executor.py:113` `_execution_stream_log_paths`、`:324` 起显示拷贝；`memory/session_archive.py:347` `events_for_task` | 原始日志与脱敏 UI 摘要分开。现 archive 查询是任务+LIMIT，不提供规范 after_cursor 回放。需要限量读取、增量游标、背压与敏感信息过滤。 |
| 停止与取消 | 内部能力已有，按运行授权需补齐 | `bio_agents.py:25093` `cancel_managed_run`、`:25139` `_commit_cancellation`；`execution/executor.py:277` `terminate_active_processes` 与执行协程取消处理；`agent_core/workflow/managed_stop.py` | 前者要求可信的当前 asyncio.Task 和归属；后者是进程级全局集合，不能直接暴露为任意 session 的停止 API。受理请求不等于子进程或集群作业停止；最终状态须等待 runtime 回执。 |
| 文件和产物 | 元数据已有；**远程文件访问边界缺失** | `memory/persistence.py:127` `save_artifact`；`memory/artifact_index.py:38` `ArtifactIndex.register_artifact`、`:67` `search_artifacts`；`agent_core/workflow/result_resolution.py`；`bio_agents.py:53106` `_terminal_status_from_plan`、`:53261` `_terminal_status_from_snapshot`、`:53403` `_interactive_run_result` | 文件存在、退出码为 0、候选 artifact 都不等于最终完成。保留运行时核验决定。新增授权工作区根、资源 ID、符号链接检查及限量预览/下载，不把 artifact resolver 当文件 ACL。 |
| 已有 GUI / HTTP / RPC | 在已查入口范围**未发现可用的工作台服务** | scoped 搜索 `bio_agents.py`、`agent_core`、`execution`、`integrations` 的 HTTP/WebSocket/RPC 路由；`agent_core/workflow/pi_rpc.py`、`step_worker.py` 和 `integrations/pi_step_worker/biomind_pi_rpc_poc.ts` | Pi JSONL POC 是 step-worker 的 StepRequest/StepOutcome 通道，不是 BioMind 会话服务；不扩展为新的运行控制器。 |

## 已有源码如何复用

本轮原型没有 import 或复制生产 runtime。以下为下一阶段复用位置，并不是本轮已经完成的集成。

| 来源 | 可复用内容 | 仅补的边界 |
| --- | --- | --- |
| `bio_agents.py` 与原图节点 | 模型调用、流程编排、Recovery、审批后的继续执行、终态 | agent 生命周期托管、明确提交/观察分离、输入通道 |
| `human_io.py`、`middleware.py` | 人工等待、拒绝/批准语义与审批权威 | 结构化输入端口；保留 CLI 默认通道 |
| `plan_tracker.py`、`action_runtime.py` | 计划身份、CAS、审批 lineage、脚本运行绑定 | 只读投影和原校验函数调用，不重建 TaskSpec |
| `cli_display.py`、`provider_attempt.py`、executor | 公共显示安全规则与观察事件 | 独立 sink、脱敏、事件落盘/游标；显示失败不能影响执行 |
| persistence/archive/artifact/result resolution | 会话历史、元数据、核验回执 | 分页、工作区授权、资源访问；不将完整内部记录透传 |
| `RunProfiler` 与 managed cancellation | 归属证据、心跳、终止事实 | 完整生命周期锁与按 owner 受理操作 |

DeepSeek 上游实际读取的文件、固定提交与动态页面区别、MIT 版权和零代码复制说明，见 [原型参考与复用表](../../../apps/remote-workbench/THIRD_PARTY_NOTICES.md)。

## 尚未核验

没有访问真实会话数据库或运行日志，没有调查现存任务/进程，更没有通过真实执行验证：多客户端同时附着、进程崩溃后恢复、实际模型 token 流、远程作业停止、集群调度器生命周期、真实工作目录权限、SSH 身份/跳板机策略、Windows/Tauri/IME/打包签名。源码可以证明内部函数存在，不能证明这些运行条件已经满足。本文缺失结论限于本轮检查的接入入口范围。
