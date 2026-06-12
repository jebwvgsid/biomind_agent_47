# BioMind 智能体记忆系统设计与实现

## 摘要

面向生物信息学分析任务的智能体系统通常需要在多轮对话、长流程执行、工具安装、错误恢复和结果复用之间保持连续状态。由于大语言模型本身受上下文窗口限制，并且缺乏可审计、可追溯的长期状态管理能力，BioMind 在智能体框架中设计并实现了一套分层、证据约束、可检索的记忆系统。该系统将运行时上下文压缩、派生任务摘要、稳定核心记忆、SQLite 会话归档以及项目级 Wiki 记忆进行解耦，并通过稳定提示词与动态提示词的分离机制控制记忆注入范围。系统在任务执行过程中将外部可观察事件实时写入 SessionArchive，Wiki ingest 再从 archive 读取未整理事件生成证据化页面和 claims；任务 Markdown 只是从 archive/wiki 渲染出来的人类可读报告。项目事实必须绑定 session_archive、task 或 artifact 等证据来源，从而降低错误记忆被长期固化的风险。当前实现已通过记忆相关单元测试，覆盖上下文压缩、核心记忆边界、Wiki 证据索引、会话恢复与提示词注入等关键行为。

关键词：智能体记忆；BioMind；生物信息学智能体；检索增强生成；长期记忆；证据约束；上下文压缩

## 1. 研究背景与问题定义

生物信息学任务具有流程长、工具链复杂、输入输出文件多、环境依赖强和错误恢复频繁等特点。例如单细胞转录组分析需要持续跟踪样本对象、质控阈值、归一化方法、聚类参数、差异分析设计以及中间产物路径；染色质三维结构分析需要维护矩阵分辨率、TAD 或 loop 结果、比较组关系和可复现命令。若智能体仅依赖当前对话上下文，在任务持续时间较长或中断恢复时容易出现三类问题：其一，早期关键参数被上下文窗口截断；其二，模型将过期信息误认为当前事实；其三，无法说明某一结论或路径来自何处，影响科学计算场景下的可复现性。

因此，BioMind 记忆系统的目标不是简单扩大提示词长度，而是构建一种面向科学分析任务的外部状态管理机制。该机制需要满足以下要求：

1. 连续性：智能体能够跨轮次、跨会话恢复任务目标、已执行步骤、产物文件和失败原因。
2. 可追溯性：项目事实、方法参数、数据路径和科学结论必须保留来源证据。
3. 可控性：不同类型记忆具有明确边界，避免把临时输出、噪声日志或未经验证的推断写入长期核心记忆。
4. 高效性：在上下文长度接近模型上限前自动压缩旧消息，仅将与当前步骤相关的记忆注入提示词。
5. 安全性：持久化前对密钥、令牌、密码等敏感信息进行脱敏，避免泄露到长期存储。

## 2. 总体架构

BioMind 采用分层记忆架构。记忆系统可形式化表示为：

```text
M = {C, T, A, W, R}
```

其中，C 表示 Core Memory，即稳定核心记忆；T 表示 Task Summary，即由事件和 claims 派生的人类可读任务报告；A 表示 Session Archive，即唯一会话事件归档；W 表示 Wiki Memory，即项目级证据知识库；R 表示外部 RAG 检索结果。系统不将所有信息混合存储，而是根据事实稳定性、证据要求和提示词注入频率进行分层。

BioMind 主智能体在初始化时创建以下记忆组件：

| 组件 | 主要文件 | 职责 |
| --- | --- | --- |
| Token-aware MemoryManager | `memory_manager.py` | 统计上下文 token，超过阈值时压缩旧消息并归档原始记录 |
| CoreMemoryStore | `memory/core_store.py` | 保存用户偏好、智能体操作规则和项目 Wiki 索引引用 |
| TaskSummaryRenderer | `memory/task_summary.py` | 从 SessionArchive 和 Wiki claims 渲染 `tasks/{task_id}.md` 任务报告 |
| SessionArchive | `memory/session_archive.py` | 使用 SQLite/FTS 保存跨会话事件，作为执行过程唯一事件源并支持 Wiki ingest 状态 |
| WikiMemoryManager | `memory/wiki_memory.py` | 从 SessionArchive 证据维护项目级 Wiki、claims、检索索引和审查队列 |
| PersistentStateManager | `memory/persistence.py` | 保存任务状态、执行记录和产物索引 |

系统运行时的记忆注入遵循“稳定提示词 + 分阶段运行时上下文”的结构。稳定提示词在会话开始时冻结 Core Memory 快照和本地 AGENTS 规则，保证一个会话中的核心上下文不因运行中记忆更新而漂移。运行时上下文并不是每轮全量注入 Task Summary、Wiki 和 SessionArchive，而是采用 Consultant 宽检索、Planner 固化约束、Generate 窄上下文执行的策略：Consultant 在任务开始前检索相似任务、历史 skill 选择、可复用环境和常见失败点，并整理为 `consultant_memory_packet`；Planner 将 packet 中有用经验写入计划约束、步骤注意事项和成功条件；Generate 默认只注入当前步骤需要的 active skill、输入产物路径、计划进度、最近 observation、环境状态和 Supervisor 反馈。只有错误恢复、任务恢复、阶段切换、缺少输入路径或用户要求证据来源时，系统才按需检索 Task Summary、SessionArchive、artifact index 或 Wiki claims。

### 2.1 设计原则

BioMind 记忆系统的设计遵循四个原则。第一是分层存储原则，即不同生命周期、不同可靠性和不同使用频率的信息不进入同一个存储区域。用户偏好、环境规则、项目事实、执行事件和科学结论具有不同稳定性，如果全部写入同一长期记忆文件，模型在后续调用中很难判断哪些信息是规则、哪些信息是旧日志、哪些信息是未经验证的推断。因此系统将记忆拆分为 Core、Task Summary、Session Archive 和 Wiki 四个主要层级。第二是证据优先原则，即项目事实只有在具有明确来源时才应被长期保存。生物信息学分析中的路径、样本、参数和结果一旦错误固化，会直接导致后续分析复现失败，因此 Wiki claim 必须绑定 session_archive、task 或 artifact 证据。第三是热路径轻量原则，即任务执行过程中只做确定性、低成本的 SessionArchive 事件追加，不在每一步都调用 LLM 做长期记忆归纳。第四是派生物可重建原则，即 Wiki 和任务摘要都从 archive 证据生成；任务 Markdown 删除后可以重建，Wiki ingest 失败时 archive pending 状态不丢失。

### 2.2 存储结构与数据流

从文件系统角度看，BioMind 运行时记忆集中存储在工作目录下的 `.biomind/memory`。其中 `USER_PROFILE.md` 和 `AGENT_MEMORY.md` 保存稳定偏好与操作经验；`projects/{project_id}.yaml` 保存项目索引字段；`tasks/` 保存从 archive/wiki 渲染出来的人类可读摘要；`session_archive.sqlite3` 保存结构化会话事件和 Wiki ingest 状态；`archive_large_outputs/` 保存过大的 stdout、payload 或内容引用；`wiki/` 保存项目知识页面；`wiki/cache/` 保存 digest、claims index 和社区结构等派生索引。历史 `raw/`、`history/` 或 `dreams/` 文件可作为旧数据保留，但不再是运行时写入路径。

系统的数据流可以概括为“观测先归档，事实后整理”。当智能体进入 consultant、planner、generate、execute、supervisor 等节点时，系统会将当前阶段、消息片段、payload、执行结果或计划状态写入 SessionArchive。此时系统并不立即把事件改写为长期事实，而是只保留可追溯结构化记录。随后用户或系统可触发 `/wiki ingest`，将 archive pending events 批量转换为证据化 Wiki 页面、claims、review queue 和 digest；也可触发 `/task summarize [task_id]` 重新渲染任务报告。下一轮执行时，Consultant 负责宽检索并把相关历史压缩成跨阶段 packet；Planner 把这些历史经验固化进计划；Generate 的普通 PASS 路径不再重新检索 Wiki 或 Task Summary，只在错误恢复、缺少输入路径、证据查询等触发条件下补充检索。

这种数据流的优势在于，它将“记录发生了什么”和“判断什么值得长期保存”两个问题分离。前者是确定性记录，要求完整、快速、低风险；后者是语义整理，要求谨慎、可审查、可回滚。对于科研计算智能体而言，这种分离尤其重要，因为执行日志中存在大量临时输出、报错堆栈和中间尝试，如果未经筛选直接进入长期记忆，会导致后续推理被噪声干扰。

## 3. 上下文压缩与归档机制

BioMind 在 `MemoryManager` 中实现 token 感知的上下文压缩机制。系统默认最大上下文预算为 200000 tokens，当消息总量达到阈值

```text
sum(tokens(messages)) >= alpha * L
```

时触发压缩，其中 L 为最大 token 预算，alpha 默认取 0.85。压缩过程首先保留稳定系统提示词、已有压缩摘要、受保护计划消息和最近约 40000 tokens 的对话，其余旧消息进入归档集合。归档消息会被写入 JSONL 文件，同时由 LLM 生成结构化摘要并重新注入为一条系统消息。

摘要提示词被专门设计为生物信息学场景适配模板，要求保留任务目标、输入数据、分析上下文、工具环境、命令参数、输出产物、结果指标、错误修复、当前状态和下一步动作。对于 scRNA-seq、甲基化、TAD、蛋白质组、代谢组和工具安装等场景，摘要模板进一步要求保留对应领域的关键参数。该机制的作用是将历史对话从“完整文本”转换为“可恢复执行状态”，避免单纯摘要造成参数丢失。

压缩后，系统维护 `memory_index.json` 和 `compression_audit.jsonl`。前者记录压缩块、关键词和归档路径，后者记录压缩前后 token 数、归档消息数量、摘要提示词和生成摘要。归档检索采用关键词重叠与文本命中的轻量评分：

```text
score(q, c) = 2 * overlap(q, keywords_c) / |q| + 0.5 * text_hits(q, summary_c)
```

这使系统在不调用额外向量服务的情况下也能检索历史摘要，并在后续任务恢复或错误排查中按需加载原始归档。

### 3.1 压缩策略的实现细节

上下文压缩不是简单删除早期消息，而是一个有保护对象的选择过程。系统首先扫描消息序列，识别稳定系统提示词、已有压缩摘要和受保护计划消息。这些消息承载智能体行为规则、已压缩历史和任务执行计划，不能被普通压缩策略移除。随后系统从消息尾部向前累计最近消息的 token 数，直到达到 `keep_recent_tokens` 上限。这样可以保证智能体仍然看到最近的用户反馈、执行 observation 和错误信息。未被保留的消息进入 archived_msgs，既被写入原始 JSONL 档案，也被送入摘要提示词生成结构化总结。

该策略本质上解决了长任务中的两个矛盾。一方面，旧对话不能无限留在上下文中，否则会挤占当前任务所需的工具文档、技能说明和执行反馈；另一方面，旧对话不能无痕删除，否则任务恢复会丢失关键路径、参数和错误修复经验。BioMind 的做法是保留“短期可执行上下文”，同时将“长期历史上下文”转换为带审计记录的摘要。

### 3.2 摘要内容的完整性要求

BioMind 的摘要提示词不是普通聊天摘要，而是任务恢复摘要。它要求模型明确区分已完成、失败、计划中和待执行的工作；要求保留精确文件路径、样本 ID、命令参数、运行环境、输出指标和错误信息；要求禁止推断未出现的基因组版本、样本分组或生物学结论。对于不同类型任务，摘要还包含领域特异性要求。例如，单细胞任务需要保留 Seurat 对象路径、QC 阈值、归一化方法和聚类分辨率；TAD 分析任务需要保留矩阵文件、分辨率、边界文件和比较组；工具安装任务需要保留环境名、安装命令、版本和健康检查结果。

这种摘要约束直接服务于中断恢复。当一个任务因模型上下文压缩或用户中断而需要继续执行时，智能体不需要重新读取所有原始终端输出，而是可以根据摘要判断“哪些步骤已经成功”“哪些文件已经产生”“下一步应该从哪里继续”。同时，原始 JSONL 档案仍然存在，因此在摘要不足时可以按需回溯原始记录。

## 4. 核心记忆：稳定、冻结与边界约束

Core Memory 由 `USER_PROFILE.md`、`AGENT_MEMORY.md` 和 `projects/{project_id}.yaml` 组成，定位为小规模、稳定、可冻结的核心上下文。其内容不承担项目事实库职责，而只保存三类信息：用户长期偏好、智能体稳定操作经验，以及项目 Wiki 的索引引用。例如 `project_root`、`wiki_digest_path`、`primary_pages`、`active_dataset_pages`、`decision_pages` 和 `last_compiled_at` 可进入项目核心记忆；而具体数据路径、样本元数据、方法参数、分析结论和科学判断默认不进入 Core Memory。

系统通过 `memory/policy.py` 定义核心记忆边界策略。若项目事实包含 dataset、sample、fastq、bam、vcf、parameter、threshold、result、decision、method、workflow 等细节词，或值表现为路径、长文本，则被判定为 Wiki claim，而不是 Core fact。被拒绝的条目写入 `CORE_REVIEW.md`，供人工或后续流程审查。

Core Memory 的另一个关键特性是会话冻结。BioMind 在任务启动时读取 Core Memory 并生成 `MemorySnapshot`，随后将该快照写入稳定系统提示词。会话运行中即使 Core Memory 被更新，也不会改变当前稳定提示词，除非显式 reload 或开启新会话。这种冻结机制避免了长期运行任务中提示词前缀不断变化，提高了执行可重复性。

### 4.1 Core Memory 的内容边界

Core Memory 的核心目标是“小而稳定”。它不追求覆盖所有项目知识，而是为系统提供长期行为约束和项目知识入口。`USER_PROFILE.md` 主要保存用户偏好，例如用户希望输出完整脚本、偏好中文说明、是否倾向保守执行等。`AGENT_MEMORY.md` 保存智能体操作层面的经验，例如某些 shell 启动方式不可用、某些环境调用方式更可靠、某些重复错误的修复方式等。`projects/{project_id}.yaml` 则保存项目级索引字段，而不是项目细节本身。

这种边界可以避免“核心记忆膨胀”。如果把所有项目路径、样本表、结果解释和参数记录都写入 Core，稳定系统提示词会越来越长，且内容会混合新旧事实。更严重的是，当旧路径与新路径冲突时，模型可能优先相信提示词前缀中的旧信息。BioMind 通过策略函数主动拒绝详细项目事实进入 Core，并将这些事实路由到 Wiki 或审查文件，保证 Core Memory 始终保持短小、稳定和可解释。

### 4.2 冻结快照对可复现性的意义

会话冻结机制使 BioMind 的一次任务执行具有相对稳定的系统前缀。对于智能体系统而言，提示词前缀类似“运行时配置”。如果任务执行过程中记忆被不断改写并立即注入，模型在同一任务内的行为约束可能发生漂移。BioMind 通过 snapshot_id 将 Core Memory 与会话绑定：当前会话继续使用启动时快照，新会话才读取更新后的 Core。这使记忆更新成为跨会话生效的稳定机制，而不是当前步骤的隐式副作用。

## 5. 派生任务摘要

TaskSummaryRenderer 负责可读、可审计的任务级报告。每个任务对应 `tasks/{task_id}.md`，但该文件不再是权威记忆源，而是从 SessionArchive 事件和 Wiki claims 渲染出来的 report/cache。任务报告包含 Goal、Current Status、Key Events、Commands and Results、Errors and Fixes、Artifacts、Wiki Claims、Open Questions 和 Source Events 等章节。删除任务报告不会删除记忆；系统可以通过 `/task summarize [task_id]` 重新生成。

这一变化移除了旧的 Markdown pending 和 Dream 整理流程。任务过程不再同时写 `logs/{task_id}.md`、`history/PENDING.md` 和 Wiki raw pending；热路径只写 SessionArchive。Core Memory 也不再由任务摘要整理器自动写入，稳定偏好和项目 Wiki 索引仍通过 CoreMemoryStore 与 Wiki promote 流程维护。

### 5.1 任务摘要的章节化组织

任务摘要采用固定章节组织，这使它既适合人类审阅，也适合后续被模型检索和重注入。Goal 保存用户最初目标；Current Status 保存当前任务状态；Key Events 保存 archive 中的高价值事件；Commands and Results 保存重要命令及结果摘要；Errors and Fixes 保存错误现象、原因和修复方式；Artifacts 保存生成的表格、图像、脚本或报告；Wiki Claims 展示引用本任务 SessionArchive 事件的长期事实；Open Questions 保存仍需用户确认的问题；Source Events 保存可追溯事件表。

这种章节化结构使任务摘要不同于原始事件。SessionArchive 关注“发生过什么”，任务摘要关注“继续任务需要知道什么”。例如，一段 R 脚本执行过程中可能产生数百行包加载信息，这些信息可通过 archive large ref 追溯，但不应进入任务摘要。相反，最终保存的 Seurat 对象路径、过滤后的细胞数量、聚类分辨率和输出图路径才是后续步骤真正需要的记忆。

### 5.2 为什么任务摘要是派生产物

任务摘要关注“继续任务需要知道什么”，SessionArchive 关注“发生过什么”。二者不应并行维护相同事件。将任务摘要改为派生产物后，恢复提示词仍能获得人类可读主线，同时所有证据都能回到结构化 archive event。这样减少了三份 pending/log/raw 之间的权威性冲突，也避免 LLM Dream patch 把任务过程误写入 Core 或长期 Markdown。

## 6. Session Archive：跨会话事件检索

SessionArchive 使用 SQLite 保存噪声较多但对恢复和证据整理有价值的事件，例如用户消息、计划生成、执行完成、错误检测、Supervisor 结论和产物创建。表结构包含 task_id、session_id、project_id、stage、event_type、timestamp、content、summary、metadata、paths、tags、source_refs、task_semantics、large_refs、content_hash、severity 和 returncode 等字段。系统优先使用 SQLite FTS5 进行全文检索，若运行环境不支持 FTS，则退回关键词检索。

SessionArchive 现在还是 Wiki ingest 的 pending 状态源。`wiki_ingest_events` 表记录哪些 archive events 已经被 Wiki 处理；`pending_for_wiki(project_id)` 返回未处理事件；`mark_wiki_ingested()` 在 Wiki patch 成功后标记已处理。事件写入前会计算稳定 content_hash，完全重复的事件直接返回既有 id。过大的 stdout、payload 或正文会外置到 `archive_large_outputs/`，数据库只保存预览、SHA256、字符数和路径引用。

会话恢复时，系统不是简单取最近 N 条日志，而是通过 `resume_events` 对事件进行优先级排序。计划创建、产物创建、Supervisor 结论、用户消息、任务完成和错误事件具有较高优先级；失败的 execution 事件会提高权重；最近一次成功执行也会被保留。这样可以在恢复提示词中突出“下一步需要什么”和“哪些步骤已经成功”，而不是注入大量低价值终端输出。

### 6.1 Session Archive 与任务摘要的区别

任务摘要是面向人类和模型的阅读层，而 Session Archive 是面向检索和证据引用的事件层。二者不再并行写入同一事实，而是形成上下游关系：Session Archive 保存结构化事件，TaskSummaryRenderer 根据这些事件和 Wiki claims 生成摘要。恢复任务时，系统优先使用任务摘要提供主线，再用 Session Archive 补充执行证据和关键事件。

例如，一个任务中可能执行了 20 个 shell 脚本，其中只有 5 个对最终结果有影响。任务摘要会写出这 5 个步骤的主线和产物路径；Session Archive 则保留每次执行的 returncode、summary、metadata 和 tags。当后续模型需要定位某个错误发生在哪个阶段时，可以检索 Session Archive；当用户需要快速知道任务做到了哪一步时，可以阅读 Task Summary。

## 7. Wiki 记忆：证据化项目知识库

WikiMemoryManager 是 BioMind 项目级长期知识的核心。它将 SessionArchive 事件、结构化页面、claims、检索索引、digest 和审查队列组合为一个证据化知识库。Wiki ingest 不再读取 `raw/conversations/*.jsonl`，而是调用 `SessionArchive.pending_for_wiki(project_id)` 获取未整理事件。每个 durable claim 默认引用 `session_archive:<id>`，历史页面中的 `raw:` evidence 仍可被读取和保留，但新 ingest 不再生成 raw evidence。

Wiki 页面采用 Markdown + frontmatter 形式，页面类型包括 concept、project、method、dataset、entity、tool、decision、issue、artifact、query、source 和 synthesis。系统要求 claim 必须包含 evidence，并且新 evidence 引用应以 `session_archive:`、`task:` 或 `artifact:` 开头。缺少证据、低置信度、冲突或 stale 的 claim 不会直接写入页面，而是进入 `wiki/review/PENDING.md`。

Wiki ingest 采用两阶段 LLM 流程。第一阶段由 wiki analyst 分析 pending SessionArchive evidence，输出 source summary、entities、concepts、claims、conflicts、patch plan 和 research queries。第二阶段根据分析 JSON 生成受限 wiki patch，只能使用 create_page、append_section、merge_section_items、upsert_claims、update_frontmatter、add_research_queries、flag_review 等白名单操作。patch 应用后系统重建 index、同步 SQLite 搜索索引，编译 digest，并在 archive 中标记事件已处理。

Wiki 检索采用混合策略。首先使用 SQLite FTS 或词法检索匹配页面标题、正文、来源、链接和任务语义；若配置了 embedding API，则计算页面向量并进行余弦相似度检索；随后使用 Reciprocal Rank Fusion 融合词法排名与向量排名：

```text
RRF(d) = scale * sum_r 1 / (k + rank_r(d))
```

最后系统根据页面链接、related 字段、任务语义相似性和社区结构扩展相关页面。Wiki digest 则保存项目页面数、claim 数、审查项数量、高置信度 claims、页面类型分布、实体/概念页面、社区结构和桥接页面。需要强调的是，digest 仅作为紧凑索引，不作为事实证据；真正的项目事实仍应来自具体 Wiki page 和 claim evidence。

### 7.1 Wiki 页面模型

Wiki 页面由 frontmatter 和正文组成。frontmatter 保存机器可读字段，包括 title、type、project_id、status、created_at、updated_at、sources、aliases、tags、related、confidence、task_id、workflow_id、analysis_type、skills、samples、output_artifacts 和 claims 等。正文则保存人类可读解释、方法说明、发现摘要或源事件预览。通过这种结构，Wiki 同时具备文档可读性和程序可检索性。

页面类型的划分对降低知识重复非常重要。工具页面记录软件包、命令行工具、数据库或外部服务；方法页面记录分析方法、流程和参数策略；数据集页面记录样本集合或输入数据源；实体页面记录跨页面出现的生物对象、样本、组织、细胞类型、路径或参数；概念页面记录抽象分析概念或生物学概念；decision 页面记录经过证据支持的分析决策；source 页面记录 SessionArchive event 的摘要。系统提示词中明确要求优先选择具体页面类型，例如软件应进入 tool 页面而不是 entity 页面，分析流程应进入 method 页面而不是 concept 页面。这可以避免同一知识被分散写入多个不一致页面。

### 7.2 Claim 的证据链约束

Wiki 中最关键的数据结构是 claim。一个 claim 通常包含 id、text、status、confidence、evidence、supersedes、review_required 和 updated_at。与普通笔记不同，claim 不是随意陈述，而是可引用、可审查、可更新的事实单元。系统要求 claim 的 evidence 必须引用可追溯来源，例如 `session_archive:42` 指向会话事件，`task:task_id` 指向任务摘要，`artifact:path` 指向产物文件；历史 `raw:event_id` 引用仅作为旧页面兼容。若 claim 缺失 evidence，或 confidence 为 low，或被标记为 stale/conflict/review_required，则不会直接写入 Wiki，而会进入 review queue。

这种证据链对生物信息学任务非常关键。以“某处理组相对对照组高甲基化”为例，如果系统只记住这个结论而不记录差异表路径、比较设计、统计阈值和生成命令，后续用户无法判断该结论是否可信，也无法复现结果。BioMind 要求此类结论进入 Wiki claim，并关联原始执行事件或结果文件，从而使后续回答能够从“记忆说如此”转变为“根据某个可追溯证据如此”。

### 7.3 混合检索与图扩展

BioMind Wiki 检索需要同时处理自然语言、工具名、文件路径、样本 ID 和生物实体。因此单一向量检索并不充分。路径和样本 ID 往往更适合词法检索；方法概念和生物学描述则更适合语义检索。系统首先使用 FTS 或词法检索召回精确匹配页面，在配置 embedding 时再使用向量检索召回语义相关页面，随后用 RRF 将不同排名融合。最后，系统根据页面之间的 wikilink、related 字段、任务语义字段和社区结构扩展相关页面。

图扩展的意义在于，用户查询常常只命中一个入口页面，但真正需要的上下文分布在相邻页面。例如查询某个 scRNA 数据集时，系统可能首先命中 dataset 页面，但后续回答还需要关联的 method 页面、artifact 页面和 decision 页面。通过页面图，BioMind 可以把直接命中页面周围的高相关页面一并纳入候选上下文，提高任务恢复和知识复用的完整性。

## 8. 提示词注入策略

BioMind 将记忆注入分为 stable prompt 和 step-local runtime context。Stable prompt 包含系统身份、基本执行协议、工具上下文、冻结 Core Memory 和 AGENTS 规则。Step-local runtime context 在 generate 阶段保持窄上下文：默认包含最新环境、激活技能、当前计划、计划进度、最近 observation、Supervisor 反馈和 Consultant 传下来的压缩记忆 packet；Task Summary、SessionArchive、artifact index、Wiki claims 和外部 RAG 不再作为每步默认上下文，而是按触发条件检索。

触发式检索规则如下：

```text
普通 PASS 下一步：不检索 Wiki / TaskSummary
Supervisor WARN / RETRY 或最近 observation 失败：检索相似错误历史
进入新 workflow batch：加载新 batch 的 active skills
缺输入路径：查 artifact index 和 SessionArchive pending buffer
用户要求解释依据或证据：查 Wiki claims / digest
resume 恢复任务：查 TaskSummary + resume_events + 持久化执行历史
```

该策略的优点在于：一方面，稳定提示词保证核心约束不被临时记忆反复改写；另一方面，动态上下文使智能体能够利用最近观测和检索证据。模板中明确规定优先级：

```text
当前用户输入 > 已验证文件系统状态 > 最新执行观测 > Wiki 证据 > Core 快照
```

因此，当记忆与当前文件系统或用户输入冲突时，系统不允许直接相信旧记忆，而必须优先验证当前状态。这一点对于生物信息学任务尤其重要，因为文件路径、样本分组和中间结果经常随分析迭代发生变化。

### 8.1 稳定上下文与动态上下文的协同

稳定上下文主要回答“智能体应当如何行动”，运行时上下文主要回答“当前这一步需要知道什么”。稳定上下文中包含执行协议、工具使用规则、Core Memory 快照和本地项目规则，它在一个会话内保持不变。运行时上下文则会随着任务阶段改变：Consultant 阶段允许较宽的历史检索；Planner 阶段把历史经验写入计划；Generate 阶段默认不重复读取完整 Wiki 或 Task Summary。当 Supervisor 给出 RETRY 或 WARN 时，运行时上下文会注入错误恢复提示并检索相似错误历史；当用户触发 search 时，RAG 结果会进入知识库上下文；当用户要求解释依据时，系统才检索 Wiki claims 和 digest。

这种协同机制避免了两个极端：如果所有记忆都放进稳定提示词，提示词会越来越长且难以更新；如果所有规则都放进动态提示词，模型每轮行为可能不稳定。BioMind 通过双层提示词结构，使“长期规则稳定”和“短期证据更新”同时成立。

## 9. 运行流程

BioMind 记忆系统在任务生命周期中的作用如下：

1. 任务启动：根据用户输入生成 task_id，初始化派生任务摘要入口，创建或读取稳定 Core 快照，并构造 stable system prompt。
2. 咨询阶段：Consultant 根据用户需求、技能目录、工作流模板和 Wiki 上下文确认任务边界，并将输出写入 Session Archive。
3. 规划阶段：Planner 使用需求摘要、Consultant memory packet、必要的 RAG 结果和技能文档生成计划，并把历史经验压缩为步骤约束、注意事项和成功条件。计划文件写入 `task_outputs/{task_id}/plan.md`，计划进度持久化后可在压缩后重新注入。
4. 生成阶段：Generate 构造 step-local context，默认只使用当前步骤必要上下文，再决定 search、execute、install、ask_user 或 finish；只有错误恢复、缺路径、证据查询或恢复任务等场景才按需检索 Task Summary、Session Archive、artifact index 或 Wiki Memory。
5. 执行阶段：系统保存脚本、执行记录、stdout/stderr 摘要、returncode 和产物引用，并将关键事件写入各层记忆。
6. 压缩阶段：当上下文压力超过阈值，旧消息被摘要化并归档，受保护计划和最近上下文继续保留。
7. 离线整理阶段：用户可通过 `/wiki ingest` 将 archive pending events 整理为证据化 Wiki 页面，也可通过 `/task summarize` 渲染任务摘要。
8. 恢复阶段：`/resume` 或 go(resume=True) 会组合 Task Summary、PersistentStateManager 执行历史和 Session Archive 高价值事件，形成恢复提示词。

### 9.1 典型场景说明

以单细胞 RNA 测序分析为例，用户可能要求 BioMind 对一个 10x 数据集完成导入、质控、标准化、聚类、marker 识别和细胞类型注释。任务启动时，系统生成 task_id 并记录 archive 事件。Consultant 会确认输入路径和分析目标，并根据技能目录选择 scRNA 相关技能。Planner 生成阶段性计划后，计划文件和计划进度被持久化。每一步执行后，系统记录脚本、stdout/stderr 摘要、returncode 和输出文件路径。若上下文过长，旧消息被压缩为保留 QC 阈值、对象路径、聚类分辨率和错误修复信息的摘要。任务结束或用户触发 `/task summarize` 后，系统从 archive 和 Wiki claims 渲染任务摘要；用户触发 `/wiki ingest` 后，可作为项目事实的内容进入 Wiki claims。

再以 TAD 分析任务为例，关键记忆内容包括 Hi-C 矩阵路径、分辨率、比较样本、TAD caller、参数阈值、边界文件和差异 TAD 结果表。若这些内容仅存在于当前对话中，一旦上下文被截断，后续可视化或差异解释就容易丢失依据。BioMind 会将矩阵和结果路径记录到 Session Archive，并在任务摘要中渲染关键路径；经过证据支持的分析决策写入 Wiki decision 页面，并在后续计划阶段通过 Wiki 检索重新注入相关上下文。

### 9.2 记忆写入与读取的闭环

BioMind 的记忆不是单向写入，而是形成闭环：执行产生事件，事件进入日志和归档，整理机制生成摘要和 claims，检索机制把相关记忆重新注入提示词，新的执行又产生新的事件。这个闭环使智能体逐步积累项目知识，但又通过证据约束和审查队列控制积累质量。与普通聊天历史相比，该闭环更接近科研工作流中的实验记录本：既记录原始过程，也整理阶段性结论，并保留结论与证据之间的联系。

## 10. 创新点与技术特点

第一，BioMind 将记忆分为“可执行上下文”“任务摘要”“核心规则”“会话事件”和“证据知识库”五类，而不是使用单一向量数据库保存所有历史。该设计减少了记忆污染，并使每层记忆具备明确用途。

第二，系统引入 Core/Wiki 边界策略。稳定偏好和索引进入 Core，项目事实和科学结论进入 Wiki claims，并要求绑定证据来源。这种机制比普通长期记忆更符合科学计算对可追溯性的要求。

第三，系统采用稳定提示词冻结机制。Core Memory 在会话启动时被冻结，运行中更新不会隐式改变当前系统提示词，从而降低长期任务中的行为漂移。

第四，系统将热路径事件记录与离线 LLM 整理解耦。事件追加是确定性操作，Wiki ingest 才调用 LLM 进行事实归纳；任务摘要渲染是确定性报告生成。这样既保证执行效率，又使长期知识写入具备验证和审查流程。

第五，Wiki Memory 支持词法检索、FTS 检索、可选向量检索、RRF 融合和图结构扩展。该混合检索策略适合生物信息学场景中同时存在路径、样本 ID、工具名、参数和自然语言概念的查询需求。

### 10.1 与传统 RAG 或简单向量记忆的区别

传统 RAG 系统一般将外部文档切分后建立向量索引，在用户提问时召回相关片段。这类方法适合检索相对静态的知识文档，但并不完全适合智能体运行过程中的任务记忆。智能体记忆不仅要回答“某个概念是什么”，还要回答“当前任务执行到了哪一步”“哪些命令已经成功”“哪些文件是最新产物”“某个错误上次如何修复”“某条结论是否有证据”。这些问题包含时间顺序、状态更新、执行证据和项目上下文，不能仅依靠向量相似度解决。

BioMind 因此没有把所有历史直接写入单一向量库，而是采用结构化分层。短期上下文由 MemoryManager 压缩；执行事件由 Session Archive 保存和检索；任务报告由 TaskSummaryRenderer 派生；项目事实由 Wiki claims 管理；外部知识仍由 RAGFlow 检索。换言之，RAG 在 BioMind 中只是知识来源之一，而不是全部记忆系统。该设计使系统既能使用外部 SOP、工具文档和错误库，也能管理自身执行过程中产生的项目状态。

简单向量记忆还存在另一个问题：它通常缺少明确的写入边界。模型可能把某次失败尝试、用户临时猜测或不完整输出写入长期向量库，后续又被相似检索召回，造成错误累积。BioMind 通过 Core/Wiki 边界、claim evidence、review queue 和 archive ingest 状态控制写入质量，使长期记忆更接近可审计知识库，而不是无法解释的历史片段集合。

### 10.2 工程落地价值

从工程角度看，该记忆系统提升了 BioMind 的持续任务能力。首先，它降低了长任务中的重复执行。智能体可以从任务摘要和 Session Archive 中知道哪些步骤已经完成，避免重新运行耗时的比对、聚类或差异分析。其次，它提升了错误恢复效率。错误及修复方式会被写入 archive 并进入任务摘要或 Wiki claims，后续遇到相似问题时可以检索到历史处理方式。再次，它增强了结果解释的可信度。项目结论不再只是模型生成文本，而是可以追溯到具体 session_archive event、执行记录或 artifact。最后，它提高了系统可维护性。不同记忆层有明确文件和数据库位置，开发者可以单独检查 Core、Task Summary、Archive 或 Wiki，而不需要在杂乱聊天历史中查找状态。

对于中期答辩而言，该部分可以作为系统创新点的重要支撑：BioMind 的记忆系统不是单纯调用外部数据库，而是围绕生物信息学智能体的执行闭环设计了从观测、归档、整理、证据化到检索注入的完整机制。这一机制能够体现研究工作的工程深度和领域适配性。

## 11. 当前验证情况

当前仓库中记忆相关测试已覆盖主要行为，并已在本次整理过程中运行：

```text
pytest tests/test_memory_manager.py tests/test_md_memory.py tests/test_core_memory_store.py tests/test_wiki_memory.py tests/test_bio_agents_memory.py -q
结果：86 passed
```

测试覆盖内容包括：上下文压缩触发、压缩审计记录、摘要提示词领域适配、Core 快照冻结、Core/Wiki 边界过滤、项目事实冲突检测、Wiki 原始事件脱敏、大输出外置、claims 证据验证、低置信度 claim 审查、digest 与 claims index 编译、Session Archive 恢复事件选择，以及 generate 阶段对记忆上下文的注入。

### 11.1 后续评价指标设计

除单元测试外，后续还需要建立面向智能体任务的记忆质量评价指标。第一类指标是恢复能力指标，例如中断后继续执行的成功率、重复执行已完成步骤的比例、恢复提示词中关键路径和参数的召回率。第二类指标是证据完整性指标，例如 Wiki claims 中 evidence 缺失率、低置信度 claim 拦截率、Core/Wiki 冲突数量和审查队列处理率。第三类指标是检索质量指标，例如给定 query 时相关任务摘要、Session Archive 事件和 Wiki 页面是否被召回，以及召回上下文是否足以支持下一步执行。第四类指标是效率指标，例如压缩前后 token 降低比例、动态提示词长度、检索耗时和 Wiki ingest 批处理耗时。第五类指标是安全指标，例如密钥脱敏覆盖率、路径逃逸拦截率和不安全持久化写入拦截率。

在中期之后，可以构建若干标准任务集进行评估。例如，设计一个长流程 scRNA-seq 任务，在第 3 步和第 6 步人为中断，测试系统能否正确恢复 Seurat 对象路径、QC 阈值和下一步计划；设计一个工具安装失败任务，测试系统能否记住失败原因和成功修复方式；设计一个项目事实冲突任务，测试 Core/Wiki lint 是否能识别旧路径与新 claim 的冲突。这些评价比单纯测试函数返回值更能说明记忆系统对真实智能体任务的价值。

### 11.2 安全性与隐私保护

BioMind 记忆系统在持久化前会对常见敏感信息进行脱敏，包括 OpenAI 风格 API key、RAGFlow key、Authorization Bearer token、password、secret、cookie、private key 等。脱敏逻辑位于 `memory/patch_validator.py`，并被 Core、Session Archive 和 Wiki 相关流程复用。这样即使执行日志或 payload 中意外包含密钥，也不会以明文形式进入长期记忆、Wiki 页面或归档摘要。

此外，Wiki 文件路径写入采用安全路径解析。系统会拒绝包含 `..`、绝对路径逃逸、Windows 盘符冒号或空路径的 Wiki 页面路径，确保 LLM 生成的 patch 只能写入 wiki 目录内部。对于大文本输出，系统不会直接把完整内容塞入 SQLite event，而是外置为 large text reference，并记录 SHA256、字符数和预览。这一设计既避免 SessionArchive 过大，也保留了完整输出的可追溯引用。

安全策略还体现在记忆优先级上。系统明确要求当前用户输入、已验证文件系统状态和最新 observation 优先于历史记忆。也就是说，记忆不能替代验证；当记忆声称某文件存在时，执行前仍应通过文件系统检查确认。该原则可以减少旧路径、旧参数或过期结论造成的风险。

### 11.3 可解释性与可审查性

智能体记忆系统的一个核心问题是可解释性：当模型给出某个建议时，用户需要知道它来自当前输入、外部检索、历史任务还是项目 Wiki。BioMind 在提示词中显式区分 `<memory_context>`、`<wiki_context>`、`<session_archive>` 和 `<knowledge_base>`，使不同来源在模型输入中边界清晰。Wiki search 的格式也会展示页面路径、标题、类型、分数和 sources，从而让使用者能够追踪召回上下文的来源。

审查性主要通过 review queue 和 lint 机制实现。Core Memory 中不应出现的项目事实会进入 `CORE_REVIEW.md`；低置信度、缺证据或冲突的 Wiki claim 会进入 `wiki/review/PENDING.md`；`/memory lint` 会同时检查 Core/Wiki 边界和路径冲突；`/wiki lint` 会检查断链、缺失 sources、低置信度 claim、孤立页面和低内聚社区。这些机制使长期记忆不是不可见的黑箱，而是可以被用户和开发者持续审查、修正和维护。

## 12. 局限性与后续工作

当前系统仍存在若干可改进之处。首先，Wiki ingest 依赖 LLM 生成结构化 JSON，虽然已有 validator 与 review queue，但仍需要进一步增强 schema 校验和自动回归测试。其次，向量检索依赖外部 embedding API，未配置 API key 时系统会退回词法检索，语义召回能力受到限制。再次，目前 digest 主要作为紧凑索引，尚未建立系统化的“事实新鲜度”评分机制。最后，记忆质量评估仍以单元测试为主，后续应构建任务级基准，例如恢复成功率、重复执行减少率、错误修复命中率、证据完整率和检索精确率。

下一阶段计划从四个方向完善：一是增加自动化记忆质量评估集，模拟长流程 scRNA-seq、TAD 和工具安装任务；二是完善 Wiki claim 生命周期，支持 superseded、deprecated 和 conflict resolution 的可视化管理；三是将高频、可复用的方法性经验从记忆迁移为 Skill，进一步区分事实知识与程序知识；四是优化混合检索排序，使路径型查询、样本型查询和概念型查询采用不同权重。

## 13. 小结

BioMind 智能体记忆系统围绕“长流程可恢复、事实可追溯、提示词可控、记忆可审查”四个目标构建。通过 token-aware compression 保证上下文连续性，通过 TaskSummaryRenderer 保证任务过程可读，通过 CoreMemoryStore 保证稳定偏好和索引，通过 SessionArchive 保留跨会话执行事件并作为唯一事件源，通过 WikiMemoryManager 将项目事实组织为带证据的知识库。该系统不仅解决了智能体长上下文不足的问题，也为生物信息学智能体在科学计算场景中的可复现性和可信性提供了工程基础。

## 参考文献

[1] Atkinson R C, Shiffrin R M. Human memory: A proposed system and its control processes. Psychology of Learning and Motivation, 1968.

[2] Tulving E. Episodic and semantic memory. Organization of Memory, 1972.

[3] Lewis P, Perez E, Piktus A, et al. Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks. NeurIPS, 2020.

[4] Park J S, O'Brien J, Cai C J, Morris M R, Liang P, Bernstein M S. Generative Agents: Interactive Simulacra of Human Behavior. UIST, 2023.

[5] Packer C, Fang V, Patil S G, Lin K, Wooders S, Gonzalez J E. MemGPT: Towards LLMs as Operating Systems. arXiv, 2023.
