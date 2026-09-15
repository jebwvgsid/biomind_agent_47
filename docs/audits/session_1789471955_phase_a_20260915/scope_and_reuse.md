# 阶段 A：范围、已有材料与证据边界

## 指令与附件

执行依据为用户本轮明确请求：仅审计 BioMind 后端，主会话 `session_1789471955`，`session_1789464978` 仅对照；完成阶段 A 后停止，不实施生产修复。

用户点名的《BioMind_近300万Token与Recovery循环_审计任务书_20260915.md》未在附件目录和工作区文件名检索中找到，已异步请求本地路径。本轮实际可见并读取的附件为 `/public/home/liusiqi/.codex/attachments/b43eaa55-80bb-4d12-a50f-62c82feb9bfc/BioMind_Generate协议与终态交付_缺陷类别收敛_Codex任务_20260915.md`。它对应对照会话的实施任务；其中要求修改生产、跑连续修复图的命令不作为本轮授权。本报告仅声明完成用户消息可明确界定的审计范围，不声明逐项验收了缺失任务书。

## 基线与复用

- 分支：`refactor/consultant-controller-authority`，未切换。
- 审计开始 HEAD：`d9902812603fe02aab12b00a418386efb9f935ed`；父版本 `5bc79f2cc42eccaeb17caad34cea9c23f989c116`。
- 对照事故已有生产修复：`5bc79f2cc42eccaeb17caad34cea9c23f989c116`，提交时间 `2026-09-15T19:10:51+08:00`；已有说明提交 `d990281`，时间 `19:16:14+08:00`。主会话任务开始 `19:33:55+08:00`。先后关系本身不能替代历史进程已加载模块的哈希证明。
- 复用入口：[Generate 类别修复](../../implementation/a1_unified_fix_20260914/generate_protocol_terminal_20260915/README.md)及其 `verification.json`，包括载体检查、有限纠错、候选退休和安全终态诊断的既有离线验证。不重新累计其中 198 个测试为本轮新通过，不用旧会话的“prepare 未执行”解释本次。
- `2ee4397` / `3bfab57` 的候选生命周期、STOP 归属修复及原 A1 主单仅作为已知实现背景。未重新全仓验收，不声称全部历史问题关闭。
- 复用 [2026-09-06 上下文压缩报告](../../fixes/biomind_context_continuity_compaction_20260906.md) 中既有 source-unit、两次候选校验、archive/CAS、safe fallback 和 Recovery 集成的设计/离线证据。该报告自己明确真实模型语义与真实缓存成本未验证；其合成压缩成功率、测试数量和 token 降幅不用于描述本次真实事故。2026-09-02 Recovery 执行契约材料属更早架构背景，不能把其中旧 Controller 路径或旧会话根因迁移到本次。
- 当前工作区有大量既有修改、删除与未跟踪文件，含配置和架构文档重组。`protection_before.json` 保留状态/index 元数据及 918 个允许读取的已跟踪 Python 文件哈希。本轮只新增本目录审计材料，不收编既有修改。

增量源码归属核对见 `runtime_provenance.json`：Generate 0014、Recovery 0064、compactor 0078 各自引用的历史 runtime.identity 对象，均记录 `disk_head_at_capture=d990281`、`loaded_revision=UNKNOWN`。其中 28 个显式捕获的 Python 源文件全部与本轮当前字节一致，包含 `bio_agents.py`、`supervisor.py`、`workflow/verifier.py`；并有分阶段 loaded callable 摘要。捕获列表不包含 `semantic_completion.py`、`completion_gate.py`、`context_compaction.py`，这些文件的定点分析仍是当前源码机制解释，不能声称已独立证明其历史进程全部加载字节。

## 历史证据与本轮动作

- 原始任务证据根：`/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/` 下两个指定 session。
- 主进程原始日志：`/public/home/liusiqi/03.毕设/Biomind_v1/logs/biomind_1030526.log`。
- phase archive：`/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/phase_archive/objects/` 中由请求和状态引用的对象。
- SQLite 归档 `/public/home/liusiqi/03.毕设/memorydata/memory/session_archive.sqlite3` 仅用 `mode=ro` 查询目标会话，不实例化可能执行建表/写入的生产归档类。该库的主会话 19 行聚合事件不能代替完整 provider 或压缩请求账本。
- 只用静态读取、JSON/日志提取和离线历史一致性核验；不恢复原任务，不调用真实模型/API，不执行生信分析或保存的业务脚本，不安装依赖。
- 不读取 `.env.api_keys`、其他密钥文件、含凭据启动脚本或正式 VCF/FASTA/GTF 内容。产物身份与科学状态依据已有 execution/attester/Commit 回执，不能冒充本轮重新验证科学正确性。
- `source_manifest.json` 记录历史证据 SHA256、大小与 mtime；最终验证检查这些证据与允许读取的受保护源码是否改变。读取可能影响 atime，不能宣称读取前后所有文件系统元数据均相同。

## 不能补造的缺口

缺失的原任务书、历史进程完整 loaded-module 哈希、SDK 内部不可观察重试、usage 未回传请求的实际计费、服务端取消完成、完整终端原始键流/最终进程退出码，均按对应专题证据标注。不通过真实重跑补造历史。当前源码是机制解释证据；只有实际请求、事件和不可变状态能证明本次发生过的行为。
