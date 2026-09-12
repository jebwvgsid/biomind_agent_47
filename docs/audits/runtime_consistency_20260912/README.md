本轮结论：交付 HEAD 与当前分支一致，相关工作区仍有未提交差异；动态 CLI 依赖尚未部署到指定 biomind 解释器；原捕获组合的 SIGSEGV 再次出现，根因仍为 UNKNOWN。此目录只交付审计，不修改生产代码、既有测试、prompt、授权或执行器。

基线：`refactor/consultant-controller-authority`，`35781fdff9e974076390c4cc3f79c4ae7c8c97a4`，tree `57fd6885ee5ef5ab160a0a67921de2b8c69c910b`。最初 index 为空，`git status --porcelain=v1 -uall` 有 437 条既有状态。测试使用原 allowlist exporter、fake provider、合成状态与原双重离线 guard。未读取原 session、业务数据库、启动脚本、真实配置或敏感环境值；未启动真实模型/API/WGS，未安装依赖、未 push。

**运行来源不能混为一谈**

`evidence/source-matrix.json` 逐文件列出 HEAD / 工作区 SHA256，`evidence/exports.json` 给出每份隔离导出完整清单与签名。HEAD 源码测试从 Git blob 导出，保留已交付 PF/LIFE/INPUT。工作区对照仅覆盖下述 5 个安全 dirty 文件，其余文件仍来自 HEAD，包括既有 dirty 测试；不能称为整个 dirty 工作区验收。真实 config 有既有修改，本轮没有读取或装载它。

| 范围／函数 | 当前工作区相对 HEAD | 实际影响及验证范围 |
|---|---|---|
| `prompts/manager.py` / `get_planner_v2_system_prompt` 等 | 加入已有 HASH_OUTPUT_POLICY | 新建 prompt 包含不输出内部 hash 的语言指导；现行两种 Plan path_policy 保留 |
| `agent_core/planner_context.py` / `provider_correction_feedback`, `save_correction_context`, `restore_correction_messages` | 已有纠正反馈投影及旧记录兼容投影 | provider 纠正消息与 HEAD 不同；完整诊断仍存原纠正记录，旧模型/用户文字不由此重写 |
| `agent_core/node_context.py` / `control_metadata_leaks` | 已有修改收窄控制字段识别 | 不再按通用 `_hash` 后缀或字符串内容判定泄漏；明确结构字段保留 |
| `agent_core/provider_message_safety.py` / `assert_v2_llm_messages_clean` | 已有实现变为直接返回 | HEAD 的全文阻断扫描在当前工作区已停用；不能因路径协议用例通过而声称其旧扫描保证仍在 |
| `agent_core/workflow/managed_stop.py` / `explanation`, `_planning_explanation` | 已有停止状态显示说明 | 区分任务停止与总结生成状态，仅本轮范围内静态核对；不是新交付业务修复 |
| `bio_agents.py`, `plan_contract.py`, `planner_loop.py`, `plan_tracker.py`、typed/phase 文件 | 清洁 | 新候选／旧 receipt 的生命周期修复仍在 |
| `human_io.py`, `cli_plain.py`, `cli_terminal.py`, `cli_display.py` | 清洁 | 已交付输入与审批边界保留，本轮不重复改造 |
| `execution/executor.py`, `execution/runtime_contract.py` | 清洁 | 只做长命令相关源码核对与微型隔离探测 |

真实消息路径为 `bio_agents.py` 的 Planner 消息装配 → `PromptManager.get_planner_v2_system_prompt()` → `PhaseContext.prepare_messages_with_compaction()` / retained correction → `_ainvoke_node_llm`。`bio_agents.py:38628` 的 retained correction 与 `:38725` 的无 phase 恢复分支可能采用保留消息；`phase_subgraph.py:435` 的固定 system/seed 有 epoch 边界约束，不能用正在运行的旧进程或旧冻结 phase 代表新 prompt 已装载。本轮所有测试从新子进程、空合成状态开始，不通过恢复原任务验收。

新进程实际导入路径见各测试 `stdout.txt` 的 `IMPORTED_SOURCES`；模块 SHA256 见 `loaded-modules.json`，均指向隔离导出。实际 provider requests 在 `pf1-*.json`、`life1-*.json` 中逐消息保留（不是只比模板）。原始合成 full-state 文件留在 `/tmp`，提交投影保留 requests、counts、reviews、executions、图 events，并附原文件 hash。

初始六个非法 `planner_materialized` 字段 → 一轮实际纠正 → 合法 Plan：C=1、P=2、G=0。反馈生命周期 normal compact/verbose/off：每次 C=2、既有 impact=1、P=3、G=1、无害进程=1。stale provider、审批期间新输入和强送旧 action 的反例仍由原断言检查。本轮无额外旁白调用。提交树消息／生命周期 6 passed，五文件 dirty 对照加加载来源检查 7 passed，HEAD 模块与短命令探测 2 passed。首次请求的 Hash output policy 在 HEAD 中没有、dirty 中存在；实际纠正反馈经 phase 装配为 human role，仍包含六处字段诊断；本例 dirty 投影去掉了 `response_completion.parser_diagnostic.source`，六处 issue 保留。最终逐次结果及消息差异统计见 `evidence/comparison.json`、`evidence/test-ledger.json`，不能将这些重叠批次相加为唯一用例数。

**真实 CLI 环境及最小部署方案**

显式核验解释器：`/public/home/liusiqi/anaconda3/envs/biomind/bin/python`，3.12.12。该环境 site-packages 中没有 `prompt-toolkit`、`wcwidth`，查找 `prompt_toolkit` 返回 None。`pytest` 为 conda 安装 9.1.1，`langchain-core` 为 pip 安装 1.2.7。`rich` 也未安装，但生产 owner 使用 prompt_toolkit；本轮不将 rich 当作必要缺项。依赖声明仅有 `requirements-cli.txt` 的 `prompt-toolkit==3.0.52`，尚不能当作已安装回执。

Codex 当前非 login shell 的 `python` 是 `/usr/bin/python`，与上述指定环境不同。核验使用绝对解释器、`-I -S` 和仅显式加入其真实 site-packages，避免用户 site hook/PYTHONPATH/临时缓存。未检查用户当前存活进程的 argv 或环境，也未推断该进程实际使用哪个解释器或 UI 开关；完整真实启动涉及被排除配置，本轮没有运行。

`probe_cli_environment.py` 调用生产 `run_terminal(runtime)`，runtime 仅输出 owner 回执，不启动 BioMind：

| 隔离条件 | 实际 owner | 缺依赖提示 | 调用 |
|---|---|---|---|
| 内核 PTY，stdin/stdout 均 TTY，TERM=xterm-256color，自动模式 | PlainTerminal | 有 | runtime=1，模型=0，命令=0 |
| 同一 PTY，仅改为强制 plain | PlainTerminal | 无（直接选择 plain） | 同上 |
| pipe，自动模式 | PlainTerminal | 无（非 TTY 直接 plain） | 同上 |

原始字节及环境白名单在 `evidence/actual-cli-fixed/`。这里没有 `/tmp/biomind-cli-deps`；业务 fake 测试沿用该缓存属于测试条件，与真实部署分开。初次审计驱动错误地传入 coroutine 而非 callable，3 个场景均 TypeError；修正的只是本目录驱动，失败记录保留于 `evidence/actual-cli/`，不是生产故障。

生产选择原因见 `cli_terminal.py:681`：显式 plain/off、TERM=dumb、任一标准流非 TTY 时直接 plain；否则构造 Terminal，ImportError 后提示并降级；运行中的绘制异常还可能走现有 `use_plain()`。缺包并非所有 plain 的唯一原因。

最小部署提案（本轮未执行）：在确认的 biomind 解释器中只安装仓库已声明的 CLI 依赖及其依赖闭包，优先使用已审查 wheel 目录；不需要全量环境升级。示意命令中的 wheel 目录必须替换为真实交付目录：

```bash
/public/home/liusiqi/anaconda3/envs/biomind/bin/python -m pip install --no-index --find-links /path/to/reviewed-wheels -r /public/home/liusiqi/03.毕设/Biomind_v1/requirements-cli.txt
```

部署后先在该解释器核对 `prompt_toolkit.__file__`、版本和 wcwidth，再在双 TTY、非 dumb、未强制 plain/off 的条件下做生产 owner 离线启动检查。审计脚本当前断言“缺依赖降级”，安装后不能直接拿它作为 TTY 成功验收。最后以确定的源码快照启动新进程／新 phase；不要清理 dirty 文件或改旧 checkpoint 来伪造一致性。

**SIGSEGV 有界实验**

先读取前轮 `planner_input_boundary_20260912` 测试账本及 `plan_results_v1_20260912/crash_notes.md`。更早 CLI V2 原 capture 已两次崩溃，曾缩到四例；独立 faulthandler / `-s` 通过都未解决根因。本轮不重跑那套旧大矩阵。

本次历史失败：tree `9b7616c0ed9ebb42d3f8d346b22f592fa0f4b219`，原三文件组合完成 21 call 后 SIGSEGV，最后是 `[copy_to_exact-off-False]` 的 setup。原冻结导出仍存在，父进程逐文件重新检查通过，既有文件未覆盖。补丁前基线 `51834d36531b641efb1c70ac3ad756a64b9a1d09` 仅导出至 `/tmp` 作对照，没有 checkout 回退。

所有实验使用原 biomind 解释器、原双 guard、原依赖缓存、原 pytest 默认捕获、原 70 秒 watchdog 与 180 秒上限。未加 `-s`、未跳过断言、未改 fixture 清理、未改变执行权威。原子进程 returncode、每个 start/setup/call/teardown、命令和父进程导出 hash 检查均单独保存，wrapper 本身的 shell exit 不替代子进程 returncode。

实验上限为 6 次（不含消息/微型 executor 验证）：原组合复现、当前 HEAD 同组合、原树八例族、补丁前八例族、当前 HEAD 八例族、原树截至历史失败的有序前缀。八例族的原测试文件及 bootstrap/runner 在三种树中逐字节相同。源码树是版本对照唯一主动变化，序列缩小另立实验，不同时改变捕获或诊断参数。

第一次原组合复现时曾与消息验收短暂并行；因此它只作为再次出现的异常证据，不能把它与单独运行的 HEAD 组合用作干净因果对照。其后崩溃实验均串行；同八例族基线／原补丁／HEAD 对照没有伴随其他测试进程。机器全局负载未读取，调度噪声仍未受控。

本轮原组合再次 SIGSEGV（19 call passed，下一项 `[copy_to_exact-off-True]` setup 未完成），停止位置与历史不同。当前 HEAD 原组合一次 48 passed；原树八例族一次 8 passed。逐次结果在 `evidence/test-ledger.json`。通过意味着该次调用完成，不能归纳为不存在间歇性崩溃。70 秒非致命 watchdog 栈不是 fatal 栈；没有取得可以归因的原生致命栈。

| 本轮实验 | child rc | 已完成 call | 耗时 |
|---|---:|---:|---:|
| crash-original-full | -11 | 19 passed | 82.38s |
| crash-head-full | 0 | 48 passed | 137.85s |
| crash-original-family | 0 | 8 passed | 95.98s |
| crash-prepatch-family | 0 | 8 passed | 96.44s |
| crash-head-family | 0 | 8 passed | 95.79s |
| crash-original-prefix | 0 | 22 passed | 94.84s |

原有序 22 例前缀本次也通过，不能证明额外两份测试文件的收集一定是触发条件；随机性、收集导入与时序仍无法分离。

最小必要前序仍未证实；已经排除“最后记录的单例必然自己崩溃”这一证据解读。默认线程池 `shutdown(wait=False)`、PlainTerminal daemon writer `close()` 不 join 仍只是源码线索，不构成崩溃原因。本轮不修生产清理，不宣称 PF/LIFE/INPUT 引入、排除或修复了 SIGSEGV。后续若继续，需要稳定复现后在相同 capture 下单独增加持久 fatal 诊断，并用单变量验证责任边界。

**配色与长命令续查**

安全 docs 中没有找到独立命名的“配色与长命令续查”报告；复用 CLI V2 / AP-OBS 报告与已提交源码，并更新至上述 HEAD。已交付输入修复不再重做。

`Terminal.item_attributes()`（`cli_terminal.py:220`）错误/失败为红色，审批/取消/未知为黄色，工具和诊断 dim；普通消息用默认属性。plain 不调用动态样式，`safe_text` 清理 ANSI；真实环境降级会解释部分“没有配色”，不能据此认定颜色代码未部署。本轮不修改配色。

`run_script_path()` await `run_command()`，返回 stdout/stderr/rc；不是后台提交后给模型一个可续查 job handle。后者同时读 stdout/stderr，记录日志、RUNNING/HEARTBEAT/PROCESS_EXITED 并等待进程。heartbeat 进入详情而非保证每次在 compact 中显示；TTY 的 1 秒 tick 仅重绘，不产生模型调用。默认 health=30s、early warning=60s、可疑短命令 stall=180s、一般 stall=600s、wall=0（无硬 wall 上限）；这些是源码默认值，本轮不读取真实环境覆盖值。

两条新建 bash 脚本分别 sleep 0.8 秒正常返回、exec sleep 4 秒但传入 0.4 秒 timeout。隔离探测检查 0.3 秒时调用仍 pending、完整原始输出与 rc、超时返回 124；只证明微型可控进程路径，不能代表长 WGS 的进展、资源、后代回收或真实 CPU 观测。guard 拒绝 /proc，因此该指标明确 UNKNOWN。探测结果见 `executor-small-probe.json`；不重构执行器、不给科学任务加入新超时策略。

**复核与复跑**

仓库根目录，生成独立新目录，禁止覆盖既有 evidence。完整 argv 在每个 `invocation.json` 和总账中：

```bash
python docs/audits/runtime_consistency_20260912/prepare_exports.py /tmp/NEW-audit
# 下列 NEW-head 可取 NEW-audit/head；scoped-dirty-probes 为五文件工作区对照。
python tests/export_planner_boundary_fixture.py /tmp/NEW-head --tree 35781fdff9e974076390c4cc3f79c4ae7c8c97a4
python docs/audits/runtime_consistency_20260912/run_probe.py /tmp/NEW-head /tmp/NEW-results -q tests/test_planner_boundary.py tests/test_planner_lifecycle_boundary.py
python docs/audits/runtime_consistency_20260912/probe_cli_environment.py /tmp/NEW-head /tmp/NEW-cli
python docs/audits/runtime_consistency_20260912/verify_evidence.py
```

新增 `offline_probes.py` 只复制到 `/tmp` 的测试目录并加入 export manifest；原仓库 tests 未编辑。未提交原 dirty 修改。审计产物 commit 与完整 hash 由最终回执给出；目录本身不递归记录自己的 commit hash。
