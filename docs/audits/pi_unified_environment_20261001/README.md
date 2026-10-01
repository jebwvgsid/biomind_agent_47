# BioMind / Pi 联合审计 v2.0

日期：2026-10-01（Asia/Shanghai）。范围：当前运行链、两个指定历史会话、隔离接线测试；仅审计。

本报告执行《BioMind / Pi 联合审计任务 v2.0》，不拼接旧单一 Agent 任务。没有修改产品源码、dist、配置、注册库、原会话、科学结果或真实软件环境，没有安装、联网模型请求、重算 TAD/ORA、push、合并或改写历史。测试中的注册写入和模拟安装仅发生在临时目录。

**结论：已有注册、选择和安装后登记能力，但迁移尚未闭环。** ORA 会话没有查询登记，先测试 PATH，再由模型写出家目录搜索。当前两份软件库都没有 rna_env，Skill 库也没有 enrichment-analysis 的运行环境映射；不能把 Conda 中存在环境说成 BioMind 已登记。native 提示词遗漏了原领域规则中更完整的登记优先要求，Skill 正文仍要求版本检查。另一个已复现的接线缺陷是：持久选择不会自动传给 native bash，也不会自动传给 native run_command；后者显式传 env_name 才设置 prefix/bin。protected run_command 可以消费持久选择。不能靠恢复强制 handoff 解决这些问题。

## 1. 当前基线与证据等级

证据标签：**R**＝指定历史会话/既有结果；**S**＝当前可达源码与磁盘构建；**H**＝历史源码/报告；**O**＝当前版本离线请求重建；**T**＝临时目录中的受控测试；**U**＝证据不足。T 中脚本强制选择工具不代表真实模型会自然这样做。

### 1.1 版本、入口与工作树

| 项 | 核实结果 |
|---|---|
| checkout | `/public/home/liusiqi/03.毕设/Biomind_v1` |
| 起始 HEAD / 分支 | `077b64e5bf879ea582043b3a7c7b8fe61c0ed3c2` / `refactor/consultant-controller-authority` |
| 起始 dirty | 107 条 tracked 修改/删除，5005 条逐文件 untracked，staged=0；不是干净 HEAD 的运行结果 |
| 产品入口 | `/public/home/liusiqi/.local/bin/biomind` → checkout 的 `bin/biomind` → Python `bio_agents.py` → `agent_core/pi_entry.py` → runtime 的 `dist/host.js` |
| shell 启动语义 | launcher 保留调用者 cwd；Python 默认取 `BIOMIND_PI_PYTHON` 或 PATH 中 python3；host Domain Python 默认为 `/public/home/liusiqi/anaconda3/envs/biomind/bin/python`，可被显式配置覆盖 |
| Node / Pi | 私有 `.runtime/node/bin/node`（本轮测试报告 v22.22.0）；安装包 `@earendil-works/pi-coding-agent` 0.87.1；pi-ai、pi-agent-core 声明同为 0.87.1 |
| Pi 本地补丁 | `0.87.1+biomind-observer.3`；补丁清单 6 个目标当前 SHA256 均匹配，包括 bundled CLI 路径，不只检查源码路径 |
| extension | host 显式加载 `dist/extensions/biomind.js`；TUI 参数含 `--no-extensions --extension …`。JSON/SDK 路径也加载同一扩展。没有发现必须启动独立 Consult/Generate 模型进程的接线 |
| 当前默认模式 | 新 host 默认 native；旧/缺少绑定的会话按 protected 处理；已有 protected 会话不能静默降级，显式迁移仍有 busy/新请求检查 |
| Python Domain 来源 | `python_service.py` → 当前工作树 `domain_adapter/service.py`，不是复制出来的另一个 PackageManager 或旧 LangGraph |

[baseline.json](baseline.json) 给出逐项 tracked dirty 清单、untracked 分组计数、完整 status 内容摘要散列及关键文件散列。起始原始逐文件 status 保存在本轮临时取证文件，未把数千项无关路径装入提交。当前磁盘散列不能证明历史进程加载过同样字节。

关键散列（完整值见 baseline）：extension TS `4b5a8214fbf9…`、extension dist `0d616142f37c…`；host TS `25bf72c8b310…`、dist `3ef8f3cd7ce9…`；DomainService Python `d29b617c0149…`。对 host、native-workflow、stage-context、execution-profile、domain-client、model-config、native-session、extension、测试 harness 共 9 个 TS 模块进行内存转译，除 source-map 注释外均与正式 dist 相等。未在正式 checkout build；此比较包括现有 dirty TS，且不是整个项目的类型检查。[node_witness.json](node_witness.json)

**历史实际启动命令 argv、启动时配置覆盖和进程已加载字节未知。** 找到了安装入口与会话 cwd/mode/model/状态绑定，没有保留这两个进程的完整启动回执；不将当前默认参数伪装成当时的实际命令。可重建的命令形态为 `biomind [--project-dir …] [--session …]` 经 launcher 进入上述 host，而非调用旧 agent 图。

### 1.2 两案例的归属

两案 project/cwd 均为 `/public/home/liusiqi/03.毕设/benchmarks/02.测试26.9`，工具用绝对路径或本次 shell 的 cd 进入具体 benchmark；项目未因此切换。

| 案例 | session ID | request ID | Domain task |
|---|---|---|---|
| TAD | `01a0f67c-1a5d-779d-8a0b-9542b5a26c76` | `18a5771a-2a47-477b-8999-c2b898f29b9d` | `pi-945c133b1b68213b14e12ad9` |
| ORA | `01a0f68c-7b84-70ca-bfcd-d234aa37b81b` | `db4f89eb-08be-4259-ad79-95411ce778f5` | `pi-442c9199c1bbb18abf64ae5d` |

session JSONL 位于 `~/.pi/agent/sessions/--public-home-liusiqi-03.毕设-benchmarks-02.测试26.9--/`；文件名分别以 `2026-10-01T08-02-03-487Z_`、`2026-10-01T08-19-56-956Z_` 开始。Domain data-root 为 project 下 `.biomind/pi-domain`，状态在 `task_outputs/<task>/plan_state.json`。两者持久绑定为 `native / product-default`。不是 9 月 29 日同名 benchmark，也不是 9 月 30 日 loop 案例。

R：两案模型均为 `deepseek/deepseek-v4-flash`，API `openai-completions`，thinking off；send_start 记录目的 origin 为 `https://api.deepseek.com`。不读取认证文件；价格未知，不将模型配置中的零费率占位值解释为免费。

### 1.3 当前最小可达链

```text
biomind → pi_entry → dist/host → 同一个 Pi session / BioMind extension
  ├─ select_environment → DomainService.select_environment
  │    → SkillRuntimeRegistry + RegistryView + registered_runtime_reuse
  │    → runtime_evidence（task 状态）→ 工具回执 → 下一模型请求
  ├─ native bash → Pi 本地 shell → 合并 stdout/stderr / 有界工具结果
  │    （没有消费 Domain runtime_evidence 的接线）
  ├─ run_command → DomainService._exec_start → FormalCommandObserver
  │    ├─ native_command：只处理显式 env overlay；env_name 在上层解析
  │    └─ protected action_runtime：消费持久 runtime_evidence + 显式环境
  └─ install_tool（仅 Generate 激活）→ install.propose / 原宿主确认
       → install.execute → 共享 executor → verification_commands
       → PackageManager.register_environment → 同一 RegistryView.path
```

S 入口：`extensions/biomind.ts:167,211,838,865,870,1171`；`domain_adapter/service.py:46,335,441,783`；`formal_command_observation.py:232`；`agent_core/native_command.py:22`；`agent_core/action_runtime.py:1311,1517`。源码引用均指本轮工作树位置。

## 2. 两案例时间线与第一处有效偏差

以下时间为 **2026-10-01 北京时间 UTC+8**。原始 JSONL 为 UTC。行号、call ID、命令和结果摘要散列见 [case_evidence.json](case_evidence.json)。时间差是 assistant/toolResult 的持久记录间隔，可能包含调度、同批工具等待和落盘；不是精确的单进程计时。

### 2.1 ORA：没有查询登记，PATH 失败后直接宽范围搜索

| 时间 | 原始证据及行为 |
|---|---|
| 16:20:11.290 | 原始用户消息（JSONL L7）：候选基因富集，自主判断 ORA/GSEA/背景/注释，明确“R/fgsea 不可用时 ORA stage blocked_dependency_missing” |
| 16:20:12–16:20:14 | 文件和任务要求读取，读取前景/背景/annotation 信息 |
| 16:20:15.559 | `read` enrichment-analysis/SKILL.md（L27–28）；随后读 dependencies、database_decision 和 R/Python 脚本 |
| 16:20:18.981–19.039 | `call_01_I4WnGJznJ661KhjEG36t1118`：`which python3 Rscript R`、版本命令；PATH 缺少 Rscript。之前 select_environment 调用为 0 |
| 16:20:20.180–16:25:55.151 | `call_01_Hiw17O4VNl8JranfuJq22103`：家目录 maxdepth 6 的 find → `head`，接着列 Conda envs 目录，再 `bash -lc` 查询 module；观测间隔 **334.971 s** |
| 16:25:56.871–16:26:16.342 | `call_00_m6qtZ57BdGHtcCCP9gOm9784`：对 biomind/rna_env/eggnog_env/dmr_r/calder_env 逐个检查可执行文件、R 版本和 `requireNamespace(fgsea)`；**19.471 s** |
| 16:26:20.275–21.103 | 构建 TERM2GENE，0.828 s |
| 16:26:22.726–22.857 | Python 输入 QC，0.131 s |
| 16:26:24.979–30.610 | `call_00_QoypuRLPoUy4t4MkUUzU0730`：绝对 `rna_env/bin/Rscript` 执行 ORA 并显示表；**5.631 s** |
| 16:26:39–16:26:54 | 写 stage_status、GSEA 跳过说明、reproduce.sh、report.md |
| 16:26:55.894–57.215 | 历史会话确实又执行了一次 reproduce.sh，结果 diff 显示一致，间隔 1.321 s。此事实补充了用户提供的摘要；本轮没有再次执行 |
| 16:26:58.796 | 末次输出检查 |
| 16:27:02.877 | `Consult / settled / handoffConsumed=false`（L142） |

**第一处可证实的策略偏差是 L48 的 PATH/版本发现而未查询登记；L55 是范围扩大的直接决策点。** 家目录 find 不是 RegistryView 或 select_environment 内部偷偷启动的；它是 assistant toolCall 参数中的原生命令。不能只归因于模型偏好：native 的领域规则裁剪、Skill 正文示例、缺少能力到环境映射以及无独立补登记工具共同使直接 bash 路径成为现实中的默认路线。

组合命令原文结构：

```sh
find /public/home/liusiqi -maxdepth 6 -name "Rscript" -type f 2>/dev/null | head
# 然后 ls /public/home/liusiqi/anaconda3/envs/
# 然后 bash -lc "module avail R 2>&1 | head -20"，外层还有 head -25
```

没有 timeout 参数，也没有分段计时。find 的前 10 条中 7 条落在 Conda pkgs 缓存、3 条是其他环境；**rna_env 出现在后续 envs 目录清单**。`head` 会截断输出，特定情况下也会令上游 SIGPIPE，但不能保证时间边界；maxdepth 不限制目录数量。335 秒不能全部归给 find，登录 shell 初始化/module 的贡献未知；同批读取 database_decision 的结果也在约 335 秒后一起持久化，不能将两条间隔相加。

R/fgsea：当前 R 脚本 `run_enrichment.R:136,148,171,279` 的 ORA 分支使用 base R `phyper` 和 BH，fgsea 仅用于 GSEA。dependencies 文档也这样区分。**但本案原始请求额外指定了 R/fgsea 停止条件，因此该检查具有用户依据，不能建议直接删除本案 fgsea 检查。** 可优化的是先根据登记/明确候选选定 rna_env，只检查明确缺失的证据，不遍历五个候选；普通未附加此条件的 ORA 不应例行检查 fgsea。

结果边界：既有 QC 表前景 8、背景 32、映射率 1；富集表 6 个 term，过滤表 1 行显著结果；report/reproduce/图/标准表均存在。GSEA 是数据不适用而跳过。本轮核对原工具回执及有限小文件，没有重算统计或重新验收整个科学结论。[artifact_metadata.json](artifact_metadata.json)

### 2.2 TAD：成功证据与托管缺口独立判断

| 时间 | 原始证据及行为 |
|---|---|
| 16:02:59.310 | 用户请求 leaf/bud 40 kb TAD，禁止省略 preflight，缺 hicexplore/cooler 则 blocked |
| 16:03:00.705 | 读 TAD Skill（L13–15）并列输入路径 |
| 16:03:04.036–43.000 | `which` 多个命令 + `conda env list`；约 38.964 s；未调用 select_environment |
| 16:03:44.607–16:04:33.094 | hicexplore 候选命令版本/import cooler 等；48.487 s |
| 16:04:35 | 读取 mcool 可用分辨率；确认含 40000 |
| 16:04:42.263–45.105 | `--preflight-only`，2.842 s；两样本 balance 检查成功 |
| 16:04:46.944–16:10:29.609 | `call_00_RcAzSMT9bfCt7cqbgOAZ8325` 原生 bash 正式执行，`timeout=3600`，末尾 `2>&1 \| tail -40`；342.665 s |
| 16:10:31–32 | 读 run summary、统计表、BED 头和数量、样本日志 |
| 16:10:42–47 | 写 report/reproduce/stage_status 并检查交付路径 |
| 16:10:49.633 | `Consult / settled / handoffConsumed=false`（L92） |

R：两样本 `tad_run_summary.tsv` 均 `success / return_code=0`；domains 1174/1157，boundaries 1177/1160。S：`call_tads.py:506–513,617–621` 使用 argv 列表 `subprocess.run`，直接把 **hicFindTADs 子进程** returncode 写入样本记录。因此本案不仅有管道末端的成功，还有独立样本进程退出证据。该脚本与产物的历史完整字节绑定没有被保存，报告保留这一限制。

原生 bash 默认 `/bin/bash -c`（本机配置解析可落到可用 bash），未自动开启 pipefail；本案命令没有 `set -o pipefail`。Pi isError=false 对应整个 shell 的退出，不能独立证明管道上游成功。T 复现 `false | tail -1` 正常返回，启用 pipefail 才报错。后写的 reproduce.sh 含 `set -euo pipefail`，不能倒推首次命令也有它。ORA 正式命令后的 echo/cat 同样可能覆盖 shell 退出，模型显示的 `exit=0` 和实际结果内容是另两种证据。

输出延迟的可证实机制：Pi bash 已有初始更新及节流 onData 更新；本轮合成 `start → 0.2s → end` 出现 3 次更新，不是只能结束后返回。TAD 子进程 stdout/stderr 写到 `leaf/bud.hicFindTADs.log`，外层 Python 打印还可能受缓冲影响，`tail -40` 对有限输出流通常等 EOF 才显示末尾。**历史没有工具 update/屏幕逐帧记录，无法分摊科学程序、管道、缓冲和 UI 各自延迟；不能报告“Pi 完全不流式”。**

两案 Domain task 目录仅有 plan_state/lock/state_contract，没有该次 native bash 的 attempts/exit_status/stdout/stderr 托管记录；session 保留调用与有界结果。损失的是 Domain run_ref、持续完整双流日志、按 cursor 读取、重连后的进程归属/取消/对账、同 call 去重关联、明确恢复状态；不是完全没有任何日志或取消能力。TAD 自己的样本日志仍在，Pi 前台 bash 也支持 signal/timeout/process-tree kill。历史数据若以后纳入索引只能标为“历史证据导入”，不能伪称当时已托管。

## 3. 能力迁移矩阵与注册事实来源

### 3.1 可达性矩阵

| 能力 | native 当前主流程 | protected / 保留角色路径 | 后端终点及评价 |
|---|---|---|---|
| 登记查询 | Consult 已激活 select_environment；工具描述明确不检查 | Consult/Generate 都激活 | **存在且可用**：RegistryView + SkillRuntimeRegistry；没有全局 list_installed_tools 注入 |
| 从能力找环境 | 仅工具/包而无 env 或 Skill 映射时无法选 | 同左 | **部分迁移**：registered_runtime_reuse 要先有 declared env；旧 discover_runtime_candidates 的有界元数据候选逻辑未暴露给 Pi |
| 环境选择 | 返回 prefix、source_ref、audit；写 task runtime_evidence | 同左 | **存在且可用**，但 AVAILABLE 只说明可用登记，unknown_tools/packages/capabilities 仍可非空，不是全部需求验证成功 |
| 已有软件补登记 | 没有独立 Pi schema/tool/Domain 方法 | Generate 也没有独立入口 | **仅后端存在**：PackageManager.register_existing_tool/register_environment/bind_tool_to_environment；不能用安装空命令或直接改 JSON 代替规范入口 |
| 安装 | install_tool 已注册但 Consult 未激活；handoff 后才可见 | 仅 Generate 激活 | **部分迁移**：提案→宿主确认→执行→验证→登记可闭环，旧 InstallerAgent 不在当前路径；仍有工具激活角色耦合 |
| 必要验证 | probe_command（有时限，但 native 不保证只读/隔网）；bash 亦可 | Consult probe 有保护限制；安装 verification_commands 必填 | **存在且可用，须按用户/首次登记/实际错误触发**；普通 select 不执行 healthcheck |
| 持久化更新 | 查询结果存 task；安装/PM 写软件库 | 同左 | **部分迁移**：软件库同一位置；写入无锁/原子替换，更新字段丢失，安装两次写入存在部分状态风险 |
| 跨会话复用 | 同 registry 可读；查到后 native 执行仍需显式接线 | protected 持久选择可用于后续执行 | **部分迁移**：独立进程读取临时权威库通过；真实 ORA 找到 rna 后未补登记，新会话不会凭软件库获知它 |
| 环境落地：Pi bash | 不消费 Domain 选择；需模型自写准确启动方式 | protected 默认不提供 bash | **当前缺口**，选择回执成功并不改变 shell 环境 |
| 环境落地：run_command | 仅 env_name→ENV_PREFIX→PATH；不自动读取此前选择，也不自动应用 env_runner | registered_execution_environment→freeze→PATH/CONDA_PREFIX + runtime 挂载 | **两条链不一致**；受控测试分别证实 |
| 作业观察/恢复 | run_command/inspect/wait/read_log/cancel 已可从 Consult 用，无需 handoff | run_command 受 Generate admission；观察工具可用 | **存在且可用**，native bash 绕过此观察层，不能靠推荐文字宣称全覆盖 |

历史链：Git `5fbcce8`（2026-09-28）删除 `agent_core/installer.py` 并收口 Pi 入口；其父版本可见 InstallerAgent / StateGraph / interrupt，BioAnalyst 初始化共享 PackageManager。`077b64e5`（2026-10-01 12:58）把新 host 默认设为 native，增加 native-workflow；其他关键接线仍包含未提交修改。9 月 17 日文档中“Installer 未删除”是当时结论，不能用于今天。上传历史片段行号不冒充本地当前行号。当前 `prompts/manager.py` 仍是 PromptManager，不是持久化权威。

### 3.2 权威来源与实际路径

| 来源 | 读写与作用域 | 当前事实 |
|---|---|---|
| 软件 registry | `RegistryView.path` 是当前 Pi 查询和安装写回的同一权威文件。优先显式/config `registryPath`、`BIOMIND_PI_REGISTRY_PATH`，其次 BIO_SOFTWARE_ROOT，最后 install_root | 正常入口 allowlist 投影安装根 `.env` 中 BIO_SOFTWARE_ROOT，经 pi_launch_metadata/host 指向 `/public/home/liusiqi/03.毕设/agent_shim/software_registry.json`；当前 24 env / 13 tools |
| 安装目录 fallback | `/public/home/liusiqi/03.毕设/Biomind_v1/software_registry.json` | 当前 1 env / 1 tool，确实是另一份旧库；不等于两案实际读错它 |
| 旧 PackageManager 根 | config 的 BIO_SOFTWARE_ROOT，否则 work_dir；`__init__` 还探测 manager/root、加载 YAML、修 shim | 与 Pi fallback（install_root）有差别；不应为普通查询恢复整个构造器 |
| Skill runtime metadata | `/public/home/liusiqi/03.毕设/Biomind_v1/skills/.skill_runtime_registry.json`，43 个 Skill 记录 | 作为已有 Skill→env/runner/能力证据；tad-calling 有 hicexplore，enrichment-analysis 无登记。不是每次 read SKILL.md 自动注册 |
| Conda inventory | `~/.conda/environments.txt` | 只有已知 prefix 元数据；包含 rna_env/hicexplore。可补全已经存在的登记的位置，**不能凭 inventory 创建登记** |
| bin_shims | 软件库根下派生启动脚本；旧 register_existing_tool 创建 | 包含 Conda 激活/自定义 setup 逻辑；Pi bash 不因 select 自动接入这些 shim，也不自动给 PATH 加 bin_shims |
| task runtime_evidence | project data-root 下 task 状态，task_id 来自 session ID 散列 | 本任务选择的证据和范围，不是软件登记的全局替代；两案都没有该字段 |
| 对话/summary/memory | Pi 原生会话及按需记忆 | 路径文字不是软件事实登记；ORA report/reproduce 中记住 rna_env，不等于新会话可查询 |

S：路径解析见 `domain_adapter/registry.py:13`、host `:66–89,457`、`pi_launch_metadata.py:24`、`pi_model_config.py:20–77`、PM `:66–102`。context_snapshot 已有 registry_path，成功选择回执已有 registration_sources；应复用展示。未注册 Skill 的早返回缺少 registration_sources，应最小补齐错误回执而不是另建诊断库。

**没有证据证明本案“安装写 A、查询读 B”。** 两案 select 调用均为 0，历史进程的 override/实际 registry snapshot 未保存；当前默认入口指向既有 agent_shim 库，project/data-root 变化本身不会在该默认路径下创建新软件库。禁用 dotenv、直接运行 host 或显式 config 时可落到 fallback，因此这是需要透明报告的部署分支，不是本案已证实直接根因。

### 3.3 rna_env 到底是否已登记

R 能证明：历史命令列出 rna_env，随后绝对 Rscript 启动、R 4.5.3 / fgsea 可用输出和 ORA 结果存在。S 能证明：两份当前软件库没有 rna_env/Rscript/fgsea 对应登记，Skill registry 没有 enrichment-analysis/rna_env 映射。文件当前 mtime 早于案例可作线索，**不能代替不可变历史快照**。本轮未发现事件当时的 registry 内容散列/备份/读库回执，故“16:20 时已在 BioMind 登记”**U，不能证实**；也不能仅凭现在缺记录断言当时一定没有。

当前只读函数验证（子进程探测数=0，[current_lookup.json](current_lookup.json)）：

- 显式 rna_env → `REGISTERED_RUNTIME_NOT_FOUND`，install_required=false。
- 只给 `tools:[Rscript]` → `REGISTERED_RUNTIME_TARGET_MISSING`，并不会搜索已登记的所有能力。
- Domain select 的 enrichment-analysis → `SKILL_RUNTIME_NOT_REGISTERED / UNKNOWN`。
- tad-calling → Skill 登记 + inventory 得 hicexplore 明确 prefix，`AVAILABLE / healthcheck=not_run`，两软件库路径都可借该 Skill 记录完成解析。

记录能力边界：packages/tools/capabilities/versions、env_runner、resolved_runtime_paths、状态/来源/历史验证时间部分已有支持；语言 R 不像 Python 一样自动推导 Rscript 别名。当前工具 schema 没有 prefix、backend/host/user 范围约束；版本仅能靠 packages specifier 表达，未知版本维持 unknown，已知冲突阻止选择。底层任意 requirements.prefix 会被忽略，不能把它当作已支持的显式 prefix 选择。多个登记/Skill 显式 prefix 冲突及无 prefix 的同名 inventory 冲突能拒绝盲选。环境 `status=broken` 不在 revoked 集合，task invalidation 只作历史错误提示；已知坏记录的范围/处理规则仍需清楚，不能用每次 healthcheck 弥补。

普通查询确实不执行软件：只读 JSON、既有 inventory 和有限路径元数据，构造回执时还会 stat prefix/bin/python（对 R 任务不一定有意义）。没有 prefix 时 inventory 读取会逐条检查目录，不调用 conda/find/import/healthcheck；明确 prefix 时跳过 inventory。不能将这类路径解析描述为“本次健康通过”，也不宜每轮重复读库来代替当前任务绑定。相同 scope/source 的 runtime evidence 去重已存在。

### 3.4 安装/登记成功并不等于全链可靠

T：[python_witness.json](python_witness.json) 记录以下当前行为：

1. 临时现有 Rscript 经 **真实 register_existing_tool** 写同一临时权威库和 shim，下载/环境创建/子进程探测均为 0；新 reader 通过 inventory 解析成功。证明可复用后端能力，不证明 Pi 已暴露该入口，也没有执行 shim。
2. Domain `_install` 使用受控 executor 替身模拟安装+验证成功，登记后新 session 查得到 prefix；无需旧 InstallerAgent、XML、LangGraph 或额外模型调用。直接后端 Consult 能安装，限制在 extension 的 active tool set。
3. 安装返回 rc=7 时不会新增 installed_fixture 登记，状态 execution_failed；但 registration_status 仍写 verification_required，失败表述可更准确。安装取消、跨崩溃恢复的全部分支未全面验收。
4. verification_commands 虽必填，却以安装 cwd 调共享 executor，**不自动置入所声明目标 prefix/runner/library env**。模型手写精确验证可成功，不能声称工具已保证在目标环境验证。模拟返回 0 仅证接线，不证明包真实存在。
5. register_environment 先保存一个 verified 环境记录，再由 `_install` 添加 prefix 并第二次保存。第二次 PermissionError 时向外抛错，却已留下 **verified、无 prefix** 的持久条目；若同名 inventory 恰好可解析，可能被重新选中。未发生真实软件回滚。
6. register_environment 重建字段集合，更新旧行会丢 prefix/env_runner（及不在重建字段中的 versions 等）。两个从同一旧快照构造的 writer 依次保存可丢掉对方新增记录；`_save_registry` 是 write_text，无锁/临时文件原子替换。旧 `_load_registry` 对损坏 JSON 吞错为空，Pi RegistryView 对损坏 JSON 抛错；Pi 文件缺失则得到空 view，未创建文件。这几种结果必须区分。

### 3.5 执行入口、进程和回执对照

| 维度 | Pi native bash | run_command（原 observer） | install_tool 后端 |
|---|---|---|---|
| 参数/解释器 | command 字符串；Pi shell 配置，默认 bash -c；无独立 argv schema | cmd 或 argv；共享 executor 字符串用非登录 /bin/sh，argv 不过 shell；protected 另有脚本/保护接线 | cmd 字符串与 verification_commands，使用同共享 executor；不调用旧 Installer 模型 |
| cwd / 项目 | 每次从 Pi ctx.cwd 开始，cd/export 只影响本进程 | 显式 workdir；不改变 session/project；native 无额外目录审批 | request 记录 cwd；目标 prefix 不自动成为执行 cwd |
| 启动环境 | host shell env；无 Domain 选择消费 | native 只显式 env overlay，env_name 上层转 prefix；protected 持久选择及 PATH/CONDA_PREFIX/挂载 | 安装/验证未自动消费目标 prefix 的启动描述 |
| stdout / stderr | 合并更新/返回；最多末尾 2000 行或 50 KiB，截断时写临时全输出文件 | 分离持久日志；observer 默认每次有界 12000 字符增量、cursor；共享 executor 自身也有捕获/流量限制 | 安装双流日志和逐条 verify 日志；安装回执各 6000 字符、验证各 1000 字符 |
| 退出码 | shell rc；无默认 pipefail；非零抛工具错误 | shell 或 argv 进程 rc；不是自动科学判定；native /bin/sh 管道同样要显式保存上游状态 | 按安装/验证 rc 决定登记；安装已发生后的验证/写入错误须标部分状态 |
| 超时/取消 | timeout 可选且无默认；AbortSignal/timeout 杀进程树，detached child tracking | observer/共享 executor 保留 deadline、process group、cleanup_confirmed；普通 wall timeout 默认禁用，可配置；probe 15 s；UI 返回不等于清理已完成 | 同共享 executor + cancel_event；完整安装取消/异常持久化语义尚未全面验收 |
| 作业标识/重放 | session toolCall ID；没有 Domain run_ref 或持久执行意图去重 | request/task/call→attempt/run_ref；锁、terminal receipt、重复 call 对账；未知不自动重跑 | install:call_id + permission claim/result；同动作回执重放，claim 未完成返回 reconciliation_required |
| 启动/运行可见性 | 初始空更新、流量更新和结束；历史 UI 每帧未知 | start 快速 yield 后 inspect/wait/log/cancel，持久状态可关联；不依赖模型心跳 | `_install` await shield(task)，工具执行等待到终态，虽有 log_refs/active jobs/cancel 标识，未接 observer 的完整 cursor 观察流 |
| 恢复/产物 | 会话保存调用/结果及路径文字；丢失最终回执时无托管 owner 对账 | 同 call 重连不重复作用；native frozen_files 为空，没有自动脚本冻结/全局产物验收 | 成功记录和安装日志可追溯；注册失败不等于软件副作用回滚；崩溃后的全过程未通过本轮测试 |

因此 native bash 的任意命令、托管 run_command 和安装命令目前只有部分共享能力。补日志不能通过偷偷恢复 protected 目录审批、命令重写或新调度器实现；科学完成、工具结束、模型 settled、取消四种状态继续分开。

## 4. 根因证据与影响范围

| 假设 | 判定、证据与范围 |
|---|---|
| 完全没有注册机制 | **否**。RegistryView/SkillRuntimeRegistry/PM、无探测选择、task evidence、安装登记与跨进程读取均存在 |
| 库/作用域错位导致此次搜索 | **未证实**。有两份库和 fallback 分支，但此次没有调用查询；正常当前入口已投影 BIO_SOFTWARE_ROOT。缺历史 override/快照 |
| rna 的登记或能力映射缺失 | **当前证实**。两软件库无 rna；Skill enrichment 无映射；只凭 Rscript 需求也不能反查。历史是否缺失仍 U |
| 查询工具没注册/没激活 | **当前否**。native Consult 的序列化工具集含 select_environment；历史 HTTP 工具定义未存，不能作同字节断言 |
| 模型未查询 | **本案证实**。两案整个 session 中 select_environment=0；查询失败、别名失配、回执截断未在本案发生 |
| 选择结果没有被执行使用 | **当前证实，非此次搜索直接因果**。当前 Pi bash 不接 Domain 状态；native run_command 不读持久选择，显式 env_name 后才设置 PATH；protected 路径有消费 |
| native 规则漏迁移 | **当前证实**。`:1172` 二选一 NATIVE_RULES/DOMAIN_RULES；native 仅保留 env_name 复用/不默认 healthcheck，漏掉按 Skill 选择、登记优先、禁止重复 which/version/import 的完整细则。离线重建也未出现该细则 |
| Skill/旧提示词干扰 | **部分证实**。历史 read 结果和当前 enrichment SKILL.md:100–103 都要求 Verify execution environment，并给出三次相同 Rscript --version；dependencies 又说加载不体检。没有证据显示本案叠加了旧 Installer/Generate 全文 |
| 重复探测藏进查询工具 | **当前 select 未发现**。Popen 禁止下查询、新 reader、连续选择均成功，healthcheck=not_run。不可据此把旧 PM 构造器拿来每轮初始化，它会检测 manager/加载配置/修 shim |
| 查询错误被当未安装 | **本案未发生；后端存在风险差异**。Pi malformed JSON 抛错，missing 文件为空；旧 PM malformed 为空。应显式标位置/错误，不能自动重装 |
| 版本/节点/别名冲突 | 本案无调用，不能归因；当前 target name/Skill alias 有部分检查，host/backend/prefix 约束接口不足，未知包能力仍可能 AVAILABLE |
| 找到后完成持久补登记 | **本案未发现**。无补登记/安装调用、无 authority 写入命令、无 runtime_evidence；当前库仍无 rna。对话与 reproduce 路径不是跨会话注册 |

最小根因链：**native 允许直接 bash + 领域优先规则不完整 + Skill 版本检查示例 → 未调用已有查询 → PATH 失败 → 无有界发现/补登记工具路径，被模型扩成家目录组合搜索**。即使加一句“先 select”，当前 enrichment 映射仍缺失，仍需现有 PM 的补登记薄入口；即使补了记录，native 执行消费缺口也仍需解决。这是多个具体缺口，不是单个 handoff 字段问题。

## 5. Consult / Generate 职责：删除、合并、保留、下沉

当前是**同一 Pi session 的角色状态、工具集和提示词切换，加真实上下文边界操作**，不是两个必然独立模型 Agent。native 普通操作无需 handoff，所以两案在 Consult 完成是允许路径，不是失败。protected 的执行 admission 和 install_tool 等激活仍依赖 Generate，不能只改 UI 名称。

handoff_generate 由 `bindHandoff` 校验结构、回执来源、用户依据、停止条件/决定，保存 handoff；turn_end 构造 boundary、切工具、`continue:true`。stage-context 的 context_edit 会从有效上下文撤下旧 user/assistant/toolResult，保留原 session 历史，以 handoff 的原用户依据和绑定证据继续；已有 compaction 时还可写宿主非语义 checkpoint。下一 context hook 用 `request.begin(role=generate,boundary_id=…)` 同步 Domain admission。不是简单标签，也不是天然节约模型调用。

| 职责 | 当前承载 | 处理建议与最小承载位置 |
|---|---|---|
| 理解需求、读 Skill、必要询问 | Consult 提示词/工具 | **合并**到同主 agent 的短领域规则，保留按需读 Skill，不重复重述任务 |
| 原始硬约束及来源 | request.userBasis/revision、Pi transcript、bindHandoff 的原用户依据与证据 | **保留/下沉**到既有 request/session 记录；移除交接前先保证原文和后续修订持续可引用 |
| 编写、执行、纠错、如实交付 | native tools、run_command、Generate 提示词 | **合并**；保留实际回执与任务要求驱动的检查，不加全局 Validator/LLM Supervisor |
| 为角色切换存在的 goal/manifest/execution JSON、激活 continuation | handoff_generate + turn_end | **删除**强制协议/自动续轮的角色用途；非本轮代码变更 |
| 工具激活与 protected execution admission | READ_TOOLS/GENERATE_TOOLS、_exec_start | **下沉**为能力/用户意图边界：主 agent 可按需调用安装和作业工具；安装意图仍单独确认，不恢复普通命令审批 |
| task_id 创建、owner、observer 初始化 | DomainService.__init__ / 初始 request.begin / exec.start | **保留**，已经不依赖 handoff：task_id 是 session hash；owner 初始 request 已记录；observer 构造在 Domain 初始化；run owner 在实际启动时建 |
| 环境绑定和必要持久化 | select_environment→runtime_evidence、软件 registry | **保留/下沉**到现有选择/启动入口；handoff 只是引用选择回执，不是唯一绑定器 |
| handoff 专属历史裁剪 | boundaryDrafts/context_edit/checkpoint | **删除**角色驱动部分；保留真正新任务隔离、来源撤回和 Pi 自身压缩，不再用交接摘要替换持续工作上下文 |
| 结束状态 | Pi stop/settled、Domain run 状态、用户 cancel | **保留并分开表达**。settled 不认证科学结果，用户 cancel 不是环境故障，不自动重试 |
| 历史角色字段和旧会话 | session custom entries、恢复分支 | **保留只读兼容**，不重写历史或因恢复触发旧交接/已完成计算；具体新兼容方案尚未实施 |

因此可以去掉实际角色依赖，但应先把仍被 Generate 隐含控制的 install_tool 和 protected admission 放到既有能力入口。不能收窄 Consult 工具迫使它 handoff，也不能删除 request.begin/环境/作业持久事实来换表面“单 Agent”。

## 6. 最小优化建议与成本

### 6.1 建议修改现有入口，按缺口实施

**M1：合并短领域规则、解除角色激活耦合。** 在 native-workflow 与 extension 中复用一份短公共领域要求：当前适用选择/登记优先，命中即按记录启动；普通使用零发现、零版本/import/healthcheck；用户明确要求、首次补登记、明确更新或实际错误才定点检查。protected 的权限描述保持独立，避免把其目录审批话术叠进 native。删除 enrichment 正文的三次版本调用及默认环境复验指令，保留输入 QC、用户 fgsea 停止条件和 TAD preflight。主 agent 暴露现有作业/按需安装能力，再移除角色专用 handoff/continuation/context 裁剪。不要机械拼接现有 DOMAIN_RULES 和 NATIVE_RULES 全文。

**M2：补齐现有登记查询—选择—启动链。** 使用 RegistryView.path 作为唯一软件权威；将 PM 已有登记/能力候选逻辑通过 DomainService/extension 薄接入，明确区分查询、定位、补登记和安装。rna_env 的最小补登记候选是利用已保存的成功执行/用户声明，记录确切 prefix、Rscript 入口和已证实的 R/fgsea 事实；未知 fgsea 包版本保持未知，不重跑真实软件。此为后续实施建议，本轮未登记。跨 host/backend/同名多 prefix 和显式约束应显式返回冲突，不能 silently 忽略 prefix 或把 unknown requirements 写成满足全部要求。

复用 `registered_execution_environment` 的已有效选择解析，接入 `native_command`；保留显式 env_name/用户 prefix 更高优先级。启动描述应覆盖真实 runner、必要库路径与子进程，不只 PATH 拼接。为 Pi bash 提供清楚的已选启动描述；若决定自动应用，当前 Pi 工具 API 确有 spawnHook/operations 扩展点，可在现有定义上薄接共享解析，须验证它在产品 TUI/SDK 实际加载，保留原生 command/cwd/exit/timeout 语义。仅回传 JSON 或 UI prefix 不算接线完成。不得假定上一工具的 export/conda activate 会延续。

**M3：修小范围持久化和安装验证缺口。** 在原 PM 写入实现保留 prefix/runner/versions 等已有字段，合并最终记录后单次带锁原子替换；明确同名不同 prefix 的更新规则，避免两 stale writer 覆盖。安装先以声明目标的启动描述验证所需能力，写入失败返回部分成功/残留/可重试登记信息，不返回 ready，不宣称软件已回滚。安装动作已发生不能因注册失败自动重装。已有软件登记入口不应下载、升级或创建替代环境，也不必借 install_tool 走安装审批。

旧 InstallerAgent 已退出本运行路径，当前 Domain 已复用安装/验证/持久化原能力；**主 agent 按需读安装 Skill + 现有 Domain/PM 薄入口**改动更小。没有证据表明必须恢复一个可选 Installer 专家才有这些能力。未来若复杂安装确有隔离收益，可评估可选专家，但不能委派普通查询/登记，更不能新增管理 Agent 或第二份注册权威。

**M4：优先复用现有观察层。** native Consult 已能 run_command，不需为托管日志恢复 handoff。近期最小路径是让需要持久观察的计算明确使用该工具；这依赖模型选对入口，不能宣称任意 bash 已被强制覆盖。更完整的一致性方案可通过 Pi 支持的工具扩展点复用原 FormalCommandObserver/执行事件，但需证明不会制造第二作业系统或改变 shell 语义；本轮不实施、不承诺全覆盖。状态从现有启动/运行/结束/取消事件和 UI 来，沉默程序仍显示运行中，不为心跳唤醒模型。不让 `tail` 取代完整日志，不将 pipeline 最后一段的 rc 当分析 rc。

### 6.2 未命中时的有界发现建议

先查用户明确约束和现有选择 → 已有登记/已配置根/静态 inventory → 少数明确 prefix 的入口 → 确有 module 部署理由时的 module 查询 → 有理由、有明确范围的更广后备。每一步区分未找到、失败、超时和未完成，查询失败不能被写成“未安装”。不要默认登录 shell，不 source ~/.bashrc，不先扫描家目录再补 timeout。

可配置起点（建议值，不是 HPC 性能保证）：最多 3 个相关候选，已配置少量根；候选定位单项 5 s、总 15 s；必要启动/包验证单项 15 s、总 30 s；返回 ≤8 KiB，完整错误按需读。慢文件系统/确实需要的 module 可显式调整，并保留取消/子进程组证据。已登记命中不进入该预算流程；这些短预算不能套到正式 TAD 科学计算。

真实执行错误后按缺入口、缺包、库路径、挂载/节点、权限或工具错误定点诊断；取消/超时/输入缺失不自动使登记失效，不重试相同命令、不全库失效、不无授权重装。

### 6.3 请求链与完整任务成本

原始 session 保存 usage_attempt，不保存 HTTP body/headers；`usage-observer.ts:436` 明确该设计。两案均未发生 compaction entry、handoff、安装和审批请求。已对两个完整 session 逐 provider_result 去重汇总，**未把同一个 assistant usage 再加一次，也未把 send_result 的 null usage 当 0 token**。

| 指标（R） | TAD | ORA |
|---|---:|---:|
| provider_start / physical send | 13 / 13 | 21 / 21 |
| provider input（未缓存部分） | 36,842 | 31,347 |
| cacheRead（命中） | 400,768 | 794,240 |
| cacheWrite | 0 | 0 |
| 总输入＝input+cacheRead+cacheWrite | 437,610 | 825,587 |
| output | 6,622 | 9,211 |
| totalTokens | 444,232 | 834,798 |
| 压缩 / 重试额外发送 | 0 / 0（所存完整事件中） | 0 / 0 |
| 用户消息→settled 墙钟 | 470.323 s | 411.587 s |
| provider_start→result 间隔合计 | 30.609 s | 47.164 s |
| 工具调用 | bash 13/read 2/write 3 | bash 20/read 4/write 4 |

Pi 当前 OpenAI-completions usage 映射 `:1161–1184` 将 prompt_tokens 减去 cache read/write 得 input，故 input 不能误当总 prompt tokens。此处是 provider terminal 报告的计数，不是独立账单验证；价格/cost null、供应商账单结算未知。科学运行 342.665 s、环境发现 334.971 s、候选检查 19.471 s 都不是角色切换耗时；删 handoff 本身不会减少两案已有 0 次交接。

没有两案保存的精确系统提示词和工具 schema 请求体，因此用已安装同版本 Pi serializer 做 **O** 重建：当前 native 系统/Skill 索引/偏好，加 ORA 搜索前 L50 为止的原始 20 条消息，在无网络 fetch 替身中序列化；不执行历史命令，不保存完整请求体。[request_reconstruction.json](request_reconstruction.json)

该重建为约 139,699 bytes，system 51,399 字符、tool 定义 12,858 bytes；含 19 个 native Consult 工具，含 select_environment/run_command，不含 install_tool，没有重复工具名。Skill 版本检查正文在历史工具结果中出现一次，随后由原生 history 携带，而不是每轮重新 read Skill。当前偏好 loaded_at 在 extension 中被去除；动态 request_id/revision/role/状态仍注入，不能仅凭 timestamp 字段猜测 token 开销。前述数字随临时 cwd/request 字符长度可变化，是重建大小，**不是历史精确 token**。

未见每轮全局 Installed Tools 清单、旧 Installer 全提示词、或两案无条件 handoff continuation；现有成本主要包括原生 Skill 索引、工具定义、逐轮增长历史/读入正文和工具输出。session custom userBasis 重复存储不等于重复发送；必须按 serializer 看实际消息。合并后也可能因工具集/历史变大反增成本；无等价受控对照，不声称节省百分比。

## 7. 验收矩阵、复现与限制

[acceptance.tsv](acceptance.tsv) 覆盖附件 E01–E18、I01–I05、A01–A03、J01–J04、P01、C01 全部 32 项。PASS/FAIL 针对注明的当前范围；NOT_RUN 保留未执行分支。测试脚本的断言通过可能是在**复现缺陷**，不能等同未来目标验收通过。

本轮受控验证：7 个新增 Python 行为见证、5 个现有 native 集成测试、1 个现有 protected 登记消费测试全部断言通过；另有 Node 原生/角色/管道/更新/timeout 见证、9 模块内存转译比较、真实 Pi serializer 的离线请求重建。没有运行真实环境 healthcheck、没有网络安装或付费模型。中途夹具曾因不同 requirements 产生不同 scope、漏填合成 PM 属性和 TranscriptContext API 取值方式而失败，已修正夹具；native 持久选择不消费则保留为产品缺陷，未修改产品使测试通过。

主要当前 FAIL：E01/E02/E10 的 native 自动环境消费；E07 的 prefix 约束接口；E13 的 Skill 默认版本检查及 native 规则缺口；E16 的真实 rna 补登记闭环；E17 的 runner/库路径消费；I03/I04 的写入失败和覆盖风险；I05 的 install_tool 激活仍绑 Generate；J02 的裸管道上游退出码。其余通过/未测范围以矩阵为准。

最小未知证据：

- 要判断历史错库/当时已登记：需要两个 session 启动时的有效 registryPath/config 来源与当时 registry 内容或不可变散列，不需要重跑科学任务或扫描家目录。
- 要判断精确历史模型可见提示词/schema：需要当时脱敏 request body 或 prompt/tool hash；当前重建只能说明现版本接线。
- 要分解 335 s：需要原组合命令分段时间或进程事件；现有日志无法恢复，不再扫描来“补测历史”。
- 要分解 TAD UI 延迟：需要当时 tool_execution_update/屏幕事件；当前工具支持流更新不能证明当时每帧已显示。
- 安装取消/部分验证异常/中断后的全流程、protected 安装目标环境验证、故障库不可读权限分支、真实跨执行节点约束及优化后的自然模型行为，仍需后续隔离验收。

**交付/停止点。** 本目录为本轮唯一新增逻辑单元。独立本地 commit，仅显式 stage 本目录；核对原 dirty 清单以及纳入基线的源码/构建散列、两原 session/state、登记记录和产物元数据。未对全部 5112 个既有 dirty 项做起始逐字节快照，不扩大完整性结论。验证详情见 [delivery_verification.json](delivery_verification.json)，提交后复核与本地提交 SHA 在交付消息中给出。未进入产品修复；后续若获准实施，以 M1–M4 对应的具体缺口为边界，不新建控制器、第二注册系统、管理 Agent 或强制科学阶段模板。
