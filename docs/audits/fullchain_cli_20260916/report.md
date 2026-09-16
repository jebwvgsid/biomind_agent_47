# BioMind 全链路修复与 CLI 收尾验收

目标：`session_1789537621`。本目录记录隔离候选；原会话未恢复，生产部署未切换。

## 1. 总体状态与原事故

原会话前六步已有正式 Commit，build、ann、summary 执行过 rc0；`finalize_outputs` 仍待核验，终态为 `blocked / RECOVERY_NO_PROGRESS`。路径原本已是绝对路径。返工涉及 root/dataDir/config 含义漂移、宽泛 Error 匹配、统计契约与复现依赖偏差，以及 Recovery 对已有物化产物定位失败。界面把这些不同层的消息连续展开，使进度和停止原因更难分辨。

本轮复用审计 `180ab791f724e9b69397645f813536d2ff0e043d`，增量清点活动入口后实施 S1–S5、S7 与 CLI 生命周期修复。S6 保留完整稳定上下文，未采用正文去重，未修改 170k 压缩阈值。阶段 B/E、现有 warning/stop、授权、计划版本、审批、取消与文件保护继续使用原入口。没有新增业务控制器、纠错 Agent、前端或科学验收捷径。

本轮源码参考要求固定为 Codex `50d77959bf927293c4b5ddcca81d05331ae582ea`。本地没有该源码，官方远程读取也未成功，状态为 **NOT_FETCHED**。因此不能声称已逐行对照指定四个 Rust 文件；生命周期实现与验收依据 BioMind 实际源码、保存事件及用户明确要求完成。

## 2. 基线、overlay 与还原

- BASE：`8a84fea91d42f8f036d66badef36b13aec6203f8`。
- 原分支：`refactor/consultant-controller-authority`；原目录 `/public/home/liusiqi/03.毕设/Biomind_v1`。
- 候选目录 `/tmp/biomind_fullchain_cli_candidate_20260916`，分支 `fix/session-1789537621-fullchain-cli-20260916`。
- 原树初始 staged 为空，69 条 tracked 修改/删除、49 条 untracked 状态记录。数量是 Git 状态条目。
- 候选复用了相关 Python overlay 和既有未提交测试。尤其 `managed_stop.py`、`test_recovery_tools.py` 同时包含先存修改与本轮修改；提交只包含相对于保存 overlay 的本轮差异。
- 详见 `source_manifest.json`、`worktree_preservation.json`、`restoration.md`。**仅 checkout 本轮 commit 不能宣称重现含 overlay 的验证基线。** 离线验证使用白名单源码导出、已有 Python/依赖、合成配置及阻断外部调用的既有 bootstrap。
- 历史 `loaded_revision=UNKNOWN` 保持 UNKNOWN；当前磁盘指纹不能补造历史加载证明。

## 3. 实际生产修改

| 范围 | 修改与边界 |
| --- | --- |
| S1 物化定位 | Recovery registry 复用既有 binding 的事实查询，解析当前计划的适用物化版本；上游已接纳依赖与当前 attempt 待审输出分开。读取时重新检查范围与版本；已知 locator 失败同步公开不可读，文件系统可读性未测时标 NOT_CHECKED。 |
| S1 路径准入 | 删除完整路径子串作为“输入已使用”的硬证据。目录变量、拼接不再因此误拒；真实必需输入、未解析结构化引用、身份、授权与执行后条件继续校验。 |
| S2 原节点纠正 | 完整原生工具调用仅缺唯一版本常量时，严格解析原始参数后复制补键，保存原文、适配副本及回执；空值、冲突、重复键、半调用、截断仍拒收。补回既有 Generate 一次组装重试遗漏的 invocation_audit 参数。 |
| S3 操作与统计 | Skill 对新计划明确 root/dataDir/config、build 直接依赖和三分区计数单位；新增两个纯读取/核验 helper，分开进程、predictor、加载、fatal 与质量信息。没有接回 renderer，未重写冻结旧计划。 |
| S4 复现来源 | 从正式 Commit/consumption/attempt 提取批准原文与执行载体，分别保留身份、argv、cwd、必要非敏感 env、配置/脚本依赖，进入真实 Generate finalize 请求。来源检查通过不等于独立执行通过；工具精确版本未记录则明确未知。 |
| S5 进展与停止 | 当前合法取证的新内容/覆盖参与局部进展；ID、时间、空页、元数据页、相同错误不充当推进。父 incident 资源不刷新。停止投影沿用可证的已接纳 episode 或当前 episode 计数，未知范围不显示假 0。 |
| S7 配置回执 | 实际请求 metadata 记录有效策略启用/禁用、来源、阈值和因果归属；不插入稳定提示词。默认 disabled 未改；300k/450k、120/180s、8192 仍为显式候选 policy。 |
| CLI | 活动按实际 attempt/tool ID 更新，消息按 request/item/event 身份完成与重放；同文不同请求保留。核验失败、Commit、终态各自显示；晚报告回执只更新交付。详情保留原始显示副本；主屏隐藏长 stdout/协议细节。退出清理输入区，保留原始输入与审批核对。 |

完整位置和反馈出口在 `blocker_inventory.tsv`，打印来源与身份在 `cli_source_map.tsv`。固定目标报告保护与诊断目录发布沿用原实现，没有通过覆盖或改名伪装交付成功。

## 4. 组合结果与验证范围

`tests/test_fullchain_delivery_20260916.py` 使用真实 PlanTracker/CAS、物化/provenance、Recovery registry/reader、核验请求构造、Completion/Gate 与 Commit：先接纳上游，读取当前交付与上游 summary，真实内容进入 Reviewer 请求；全部条件满足后 finalize Commit。坏统计、缺依赖、失效 attempt 三种反例继续拒绝当前提交，保留上游完成事实。四条链各两次 Recovery 替身响应、零科学重派发。

Reviewer 与 Recovery 的响应是显式替身；前置科学命令是已记录的合成 leaf。真实对象读取、请求构造、状态变更、正式提交和反例拒收没有被替换。另有已安装 Bash/Python 对三条合成 VCF 记录的空输出目录独立统计复现，手算预期为 3 条 VCF、2 条含 ANN、4 个 ANN 条目；复合 SO 不拆分。删除 helper 后真实微型命令失败。这仅证明该统计 fixture 的独立依赖链。

既有 Phase E 完整图链同时验收新动作/新审批后成功 Commit，以及持续调查按父资源上限停止。停止收尾从真实持久状态进入生产 CLI；保护报告未覆盖、内部诊断归档成功、模型总结 0 次分别核对。测试矩阵、原始回执位置和实际结果见 `acceptance_matrix.tsv`、`test_results.json`。

最终专项及适用回归合计 **440 个独立测试用例通过**，按 nodeid 计数，未将重复的 unittest 子测试回执再次计入。全部最终生产文件字节相同；Generate 旧测试协议更新与历史回放是测试专用增量。扩展回归有 **14 项在修改前基线也失败**，未声称修好，见 `baseline_regressions.json` 和 `regression_notes.md`。合并长图批次曾触发离线看门狗；最终拆组完成，没有改生产预算或扩大重试次数。

## 5. 显示前后与历史重放

详见 `before_after.md` 与 `cli_source_map.tsv`。主屏区分 rc0、局部 VALID、正式核验与 Commit；同一失败 attempt 保持一个活动对象。append-only 输出已显示 rc0 后，后来确认的核验失败会追加一次必要更新，不能被“收束”隐藏。

公开正文从 delta 起保留顺序位置，completed 给出唯一最终正文；两者不一致的原始差异留在详情。事件重放按身份处理，不做全局文本去重。默认主屏不展开技术详情；活动详情独立保留，短诊断队列轮转不会丢掉该活动的完整回执。

TTY 验证调用实际 `run_terminal` 和 prompt_toolkit Application，使用可记录的测试输入/输出；plain 调用实际写入队列。另将原会话 **150 条原始公开说明/工具生命周期事件**，经生产 `_schema2_recovery_event_sink` 在 TTY/plain 各重放两次，保留 **34 个唯一工具活动**，无新增模型/工具执行。完整来源 hash、原事件 fixture 与回放结果均保存；仅写新隔离事件日志，没有恢复 Plan。它不是截图像素测试，也不声称恢复了缺失的完整历史终端录像。重复附件/片段只属于同一会话，物理请求按 call_id 去重，事件按原 event_id 去重。

## 6. 效率、上下文和缓存

同类固定版本缺键问题，BASE+overlay 与候选使用相同完整原生响应：模型请求 **2 → 1**，纯格式重发 **1 → 0**，工具调用 **0 → 0**。完整 System 保持 9,906 字符；Seed 未被摘要化。第一请求序列化字符 27,950 → 28,307（包括定位元数据与隔离路径长度差异），不能把此例描述为上下文变短。完整前后请求与成分见 `context_comparison.json`。

历史 72 次唯一请求：input 4,321,270，cached input 3,102,976，output 63,603；加权缓存输入占比 71.807%。Recovery 36 次：input 3,288,689，cached 2,735,360，output 13,612，占比 83.175%。最后 episode 首轮 input 90,586、cached 3,328（3.674%）；该 episode 十轮 input 1,045,744、cached 935,552（89.463%）。因此“每轮九万都未缓存”不成立。

`requests_by_episode.tsv` 与 `cache_episodes.json` 保留六个 episode 的首轮/后续分布。缓存列的历史可得口径为 `partially_reported_derived`，属于 input 子集；未再次加总，也不作账单。cache-write 与可信费率 UNKNOWN。日志所记请求耗时相加为全场 461.259s、Recovery 187.245s；这是已有 duration 字段的和，不是墙钟总耗时，也没有混入人工、工具或本地构造等待。未采到的独立 TTFT/本地准备拆分 UNKNOWN。

未采用正文去重：没有证明必要材料、身份与后续前缀在真实 provider 上无损。保留稳定 System、冻结 Seed、完整继承材料和局部追加历史。历史估算均低于 170k，没有压缩事件符合当时配置；本轮不将其称为压缩失效。新增证据通过现有事实投影纠正旧事实，不能为了缓存保留错误值。

## 7. 交付与限制

- 实际源码与离线逻辑提交：见 `commits.json`（验收后生成）；不 push、不部署。
- 真实模型/API canary：**NOT_RUN**，本轮未授权真实调用。
- 真实 provider 缓存复测/费用：**NOT_RUN / UNKNOWN**。
- Java/snpEff 微型科学工具：**NOT_RUN**，本轮只运行允许的 Bash/Python 合成统计 fixture。
- 原数据科学 E2E、独立重建完整 snpEff 数据库及注释：**NOT_RUN**。
- 原工作区命令 replay、原会话恢复：**NOT_RUN**。
- Codex 固定版本源码对照：**NOT_FETCHED**，远程读取不可用。

验收通过表示这些隔离边界和组合行为成立，不保证真实模型每次遵从新指导，也不把界面简洁等同于科学故障已消除。

## 8. 原会话与文件保护

原会话和原交付文件未改写，六次历史 Commit 未重做，finalize 未回填成功。原分支、索引、先存 overlay 和 Git 状态均在提交前后核对；见 `worktree_preservation.json`。历史原始归档保持原字节。完成授权修复后停止，不顺手继续科学任务。
