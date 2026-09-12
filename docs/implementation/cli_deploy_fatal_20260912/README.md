本轮执行动态 CLI 部署、生产样式验收和持久 fatal 取证，基于实际 HEAD 增量工作。执行跨 2026-09-12／13，沿用用户执行单日期命名。原任务、真实 BioMind 模型/API、WGS、原业务数据库、启动脚本与敏感配置均 NOT RUN / NOT READ。

**最终状态：DEPLOYED / VISUAL VERIFIED（以下限定范围）/ CRASH NOT REPRODUCED，ROOT CAUSE UNKNOWN。** 用户已在登录终端完成限定两包的离线安装，独立新进程核验实际路径、版本和 wheel 文件字节通过。无缓存生产 owner、显示与审批/输入验收通过。工具侧只读安装失败、初始驱动超时及一次验收脚本误判均保留；未用成功重跑覆盖原记录。

**来源与提交范围**

开始 HEAD / 上轮实际审计 commit：`86836a2ce1fd2e3d2870a01547384ffb6cad72fd`（78 个 runtime_consistency 审计文件）；其 tree 为 `89964abed21ee3ff2ce681474f279b58452ece12`。35781fd 是上轮基线，不是本轮开始 HEAD。分支为 `refactor/consultant-controller-authority`；开始 index 为空，437 条原工作区状态。

五个既有 dirty 文件保留：`prompts/manager.py`、`agent_core/planner_context.py`、`node_context.py`、`provider_message_safety.py`、`workflow/managed_stop.py`。本轮既不收编这些文件，也不恢复旧全文扫描。隔离验收采用 Git 提交树及本轮明确文件覆盖，不声明整个 dirty 工作区已验收。长命令执行器、模型回合、预算、Plan 与 typed routing 没有改动。

| 已提交切片 | 完整 commit | 文件范围 |
|---|---|---|
| 持久 fatal 测试能力 | `4983ed592f99386b9b39301ae11b132fe070490b` | tests/cli_model_progress_support.py、offline_managed_bootstrap.py、cli_fatal_plugin.py、test_cli_fatal_outlet.py、run_cli_fatal_diagnostics.py |
| 安装受阻阶段证据 | `1cef57c6b2656454e92def6a488cf1108bc33f14` | 本目录文档、安装/核验脚本及限定证据 |
| 生产样式 | `f6e8fdb60388a1cd8c9091b10a2627027c029609` | 仅 agent_core/cli_terminal.py |
| 已安装依赖及 PTY 验收 | `9432b043f753c5f6101b4f87674f241f08872e54` | 8 个 tests 文件，见下文 |

最终文档提交包含本报告及新增真实安装/验收证据，完整 hash 见最终回执或 `git log -1 --format=%H -- docs/implementation/cli_deploy_fatal_20260912`。依赖部署有独立回执，不用 Git commit 表示已安装。

8 个测试文件：`tests/cli_model_progress_support.py`、`tests/replay_approval_visibility_pty.py`、`tests/replay_input_boundary_pty.py`、`tests/test_cli_v2_display.py`、`tests/replay_cli_deployment.py`、`tests/export_cli_deployment_fixture.py`、`tests/test_cli_semantic_styles.py`、`tests/test_cli_deployment_receipts.py`。

73 项显示/输入测试及 owner 矩阵使用冻结候选 tree `2e2d9a22d9649bc02f6b62cbb1b3ad064c0bc080`；最终代码/测试提交 tree 为 `278d251240cbdd7b68cfd4882d5e512532c21114`，与最终隔离导出完全对应。两候选的生产文件逐字节相同；后者仅修改 PTY 回执检查、导出清单并添加 12 项测试，其新检查与剩余 PTY 在最终树上验收。差异见 `evidence/candidate-final-source-comparison.json`，未拿旧进程或旧 phase 代替新进程验收。

**DIAG-1：已接通持久 fatal，真实根因仍 UNKNOWN**

先核对实际 pytest 9.1.1 的 `_pytest/faulthandler.py`：其 configure 会将 handler 重定向到复制的 stderr，unconfigure 会 disable/restore。因此在原 bootstrap 自然导入 pytest 后注册新插件，sessionstart（configure 已结束）启用独立 fd；unconfigure 外层 wrapper 最后重新启用，保持到 OS 关闭进程。文件权限在运行时为 0600，fd 不与 0/1/2 共用，不经 Terminal 或日志队列。只对新合成子进程禁用 core 文件，不收集或提交原始内存。

原默认 pytest capture、双 guard、原 70 秒 watchdog、180 秒单次上限不变。新增诊断会影响时序；不称为完全无侵入。watchdog 的目标没有改成 fatal 文件，定时线程采样不是 fatal 栈。Python 3.12 只保证这里的 Python 线程栈，不承诺完整 C 回溯。

独立 self-test 在明确标记的新合成子进程中向自身发送 SIGSEGV，child rc=-11，独立 fatal.txt 包含 `Fatal Python error: Segmentation fault` 和 `test_synthetic_fatal_outlet` 栈。这验证诊断出口，不代表真实间歇性根因复现；普通测试不会执行此信号。

真实目标沿用原失败冻结树 `9b7616c0ed9ebb42d3f8d346b22f592fa0f4b219` 的相同三文件顺序，仅叠加诊断测试文件。不是回退生产工作区。串行运行两次，各 48 passed、child rc=0，耗时 124.69s / 115.90s。fatal 文件只有启用/重新接通标记，无真实 fatal 栈。没有新证据，停止重复，未运行第三次或扩大矩阵；未修改业务清理。两次结束后才开始安装 dry-run/实际安装，见 `diagnosis-install-barrier.json`。

纠正上轮证据表述：历史 `crash-original-full` 最后一项 `[copy_to_exact-off-True]` 的 **setup 已报告 passed**，未见该项 call/teardown 完成。原 JSON 保留；不能写为“setup 未完成”，也不能据最后节点认定故障指令或责任线程。`call_failed=0`、`error=null` 不抵消历史 child rc=-11。本轮没有宣称崩溃修复。

复跑命令（必须使用新的结果目录；安装后依赖条件已变，不可再称本轮安装前对照）：

```bash
python tests/run_cli_fatal_diagnostics.py --export /tmp/biomind-cli-deploy-fatal-20260912/diagnostic-original --results /tmp/NEW-fatal-selftest --selftest
python tests/run_cli_fatal_diagnostics.py --export /tmp/biomind-cli-deploy-fatal-20260912/diagnostic-original --results /tmp/NEW-fatal-target
```

**DEP-1：真实 wheel 与独立安装记录**

requirements-cli.txt 仍仅声明 `prompt-toolkit==3.0.52`。原目标解释器为 `/public/home/liusiqi/anaconda3/envs/biomind/bin/python` 3.12.12，原先缺少两个包。本轮未全环境升级，未使用 sudo、--user、--ignore-installed 或 --force-reinstall。

初次离线搜索未找到 wheel（含检查 522 个 pip HTTP 缓存包体）；没有把 `/tmp/biomind-cli-deps` 解包目录当 wheel。用户随后明确授权下载。工具网络发生 DNS 失败及 HTTPS 连接失败，未修改系统网络配置；用户在登录终端成功下载到 `/public/home/liusiqi/03.毕设/biomind-cli-wheels-20260912`。磁盘文件 SHA256 与 PyPI 官方发布元数据一致：

| Wheel | SHA256 |
|---|---|
| prompt_toolkit-3.0.52-py3-none-any.whl | 9aac639a3bbd33284347de5ad8d68ecc044b91a762dc39b7c21095fcd6a19955 |
| wcwidth-0.2.13-py2.py3-none-any.whl | 3da69048e4540d84af32131829ff948f1e022c1c6bdb8d6102117aac784f6859 |

来源：[prompt_toolkit 官方元数据](https://pypi.org/pypi/prompt_toolkit/3.0.52/json)、[wcwidth 官方元数据](https://pypi.org/pypi/wcwidth/0.2.13/json)。选择 wcwidth 0.2.13 与既有隔离验收一致，满足未限定版本的 wcwidth 依赖。其 backports.functools-lru-cache 仅适用于 Python <3.2，在目标 3.12 不生效；未安装第三个包。核对 wheel RECORD 中所有 SHA256、purelib 标记及无安装脚本/本机扩展。

`install_cli_wheels.py` 用绝对解释器 `-I -S`，显式加入真实 site-packages；PIP_CONFIG_FILE=/dev/null，另用 audit hook 拒绝联网和敏感配置。先 dry-run 再安装，要求计划仅包含两包，并核对两阶段间已安装版本清单未变；记录原有消费者，回退不自动卸载。

工具端 dry-run 成功且环境差异为空；工具端安装遇到 conda 目录只读（Errno 30），rc=1、changed={}、removed=[]，失败回执保留。这是文件系统只读错误，不是自动审批拒绝。用户随后在登录终端执行相同已核验脚本：2026-09-12T16:33:42Z dry-run 成功、16:33:56Z 安装成功，回执位于 wheel 目录下的 deployment 子目录；本报告保存副本于 `evidence/login-deployment/`。前后分发包清单核对只有 prompt-toolkit 3.0.52、wcwidth 0.2.13 两项新增，原包版本和文件位置清单无变化、无移除。

独立目标 Python 新进程 `-I -S` 核验通过，两包的 `__file__` 均位于 `/public/home/liusiqi/anaconda3/envs/biomind/lib/python3.12/site-packages/`，全部有 RECORD 摘要的 wheel 文件与实际安装字节相同，`cache_on_path=false`。完整路径、版本、逐文件 SHA256 见 `evidence/installed-verified.json`。生产 Terminal 子进程另记录实际导入模块与版本；plain/off 不必导入可选包，仍保留正确降级路径。父进程的屏幕解码器使用既有缓存，只做文本解码，不进入被验收子进程的依赖路径。

用户实际执行的两步命令（留作部署记录，已完成，不要重复安装或覆盖回执）：

```bash
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 PIP_CONFIG_FILE=/dev/null TMPDIR=/tmp \
  /public/home/liusiqi/anaconda3/envs/biomind/bin/python -I -S \
  /public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/cli_deploy_fatal_20260912/install_cli_wheels.py \
  dry-run /public/home/liusiqi/03.毕设/biomind-cli-wheels-20260912 \
  /public/home/liusiqi/03.毕设/biomind-cli-wheels-20260912/deployment

env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 PIP_CONFIG_FILE=/dev/null TMPDIR=/tmp \
  /public/home/liusiqi/anaconda3/envs/biomind/bin/python -I -S \
  /public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/cli_deploy_fatal_20260912/install_cli_wheels.py \
  install /public/home/liusiqi/03.毕设/biomind-cli-wheels-20260912 \
  /public/home/liusiqi/03.毕设/biomind-cli-wheels-20260912/deployment
```

部署验收脚本（不启动 BioMind）：

```bash
/public/home/liusiqi/anaconda3/envs/biomind/bin/python -I -S docs/implementation/cli_deploy_fatal_20260912/verify_installed_cli.py /public/home/liusiqi/03.毕设/biomind-cli-wheels-20260912 /tmp/NEW-installed-receipt.json
python tests/replay_cli_deployment.py --export /tmp/NEW-export --results /tmp/NEW-owner --case normal
```

默认 dependency-source=installed；子进程没有临时 PYTHONPATH，记录包实际 __file__、版本及 owner。正常、错误、无颜色三例选择 Terminal；plain、pipe、dumb、off 及显式 missing 五例选择 PlainTerminal，8/8 通过。`missing` 用例显式阻断可选包来保留原缺依赖负例，安装后没有改掉该负例含义。初始 `owner-not-deployed` 明确记录期望 Terminal、实际 PlainTerminal、passed=false，不改写为通过。

**VIS-1：生产 Terminal/Application 的有限显示改动**

先录 baseline 的正常说明+真实小文件读取、错误+取消，以及 42 列长候选审批+详情返回。记录确认原工具行整行 dim、错误正文整段 red、审批输入区域无加粗。之后仅修改 `agent_core/cli_terminal.py` 的格式片段与样式接点：

- 模型公开段落仍为默认前景/正常亮度，段后留一空行；不改模型内容或解释其语义。
- 工具动作青色，显示副本中的路径稍淡。明确工具 returned/failed 才使用短绿/红标记；公开文字说“完成”不会获得成功标记。
- 审批摘要、输入附近的等待状态和提示加粗黄色，保留文字和原键语义。
- 错误/取消正文恢复默认前景，只突出短标记；外部工具 ANSI 仍清理。
- 无颜色模式仍有文字与短标记；plain/off 保留其原始信息通道。

没有修改审批 payload、其 hash、接纳对象、输入 parser、执行器或模型 prompt。标记只属于显示，不能给 CompletionGate 或 PlanTracker 新证据。

`replay_cli_deployment.py` 运行真实 run_terminal/Terminal/Application，使用真实内核 PTY，并旁路记录实际 Vt100Output 属性调用；真实 ANSI/cast 一并保留。最初两个新驱动回放缺少既有隔离工具的 selector 唤醒服务，35 秒上限终止（-9）；之后复用既有 pulse 服务，保持生产输入/写入/future 不变，baseline 与候选都采用同样驱动。失败录像不删除，也不作为真实业务故障归因。它不是原生 fatal 修复。

颜色证据来自实际 ANSI（青色 36、绿标记 32+bold、红标记 31+bold、审批 33+bold）；无颜色输出仅有 reset/bold。屏幕文本重建只证明布局，不是彩色截图或像素级还原。采用终端 ANSI 调色板、默认正文前景且没有新增主题查询；明暗主题的具体 RGB 由终端配置决定，不宣称已测遍所有主题/字体。

已有测试的旧颜色断言按新样式更新，保留 flush、返回值、内容与审批断言。新测试核对公开完成声明不会变成工具成功、长错误正文中性色、路径样式和 ANSI 清理。安装后的真实审批路径复用 HITLMiddleware → _prompt_user → owner → 原 parser → 无害进程，pending 执行 0 次，明确批准后 1 次。9 个最终 PTY 场景通过：TTY 长详情返回、TTY 拒绝、plain/off 批准、TTY 空白粘贴后明确批准、独立反馈、Enter+CRLF、EOF、取消。拒绝、反馈、EOF、取消均未执行；详情/空白粘贴本身不批准。长审批回放为 42 列、430 行正文及并发输出，prompt 最后屏幕持续可见。与原 baseline 比较，批准脚本 SHA256、execute 路由、真实进程返回值和 stdout/stderr 相同；这些合成回放模型调用均为 0。

临时缓存下的初始 VIS 回放只用于定位与样式对照，明确 `deployment_evidence=false`。安装后又用相同实际依赖分别回放 baseline 的正常/错误场景，确认样式前后 runtime/tool/model 调用数和动作相同；所有 owner 开关下的相同正常场景也保持相同动作与调用数。生产前后真实 ANSI/cast、无颜色 SGR、审批加粗记录及不变性断言见 `evidence/installed-invariance-and-ansi.json` 与 `evidence/installed-approval-invariance.json`。

**保留的 PTY 验收脚本误判**：首次安装后 off 批准回放 child rc=0、原批准脚本执行一次并返回正确结果，但外层断言失败，原因是旧检查把 lifecycle 严格限定为 `RUNNING, PROCESS_EXITED`，实际收到合法 `RUNNING, HEARTBEAT, PROCESS_EXITED`。失败的 assertions/result/raw 原样保留于 `evidence/installed-approval-off-approve/`。只修测试接纳规则：必须恰好一次首项 RUNNING、一次末项 PROCESS_EXITED，中间只允许 HEARTBEAT，所有项 pid/pgid 一致，退出为 0；不删除检查或丢弃原心跳。12 项针对性测试包含 0/1/3 心跳及缺失、重复、越界、异 PID、未知状态、非零退出的拒绝。最终 off 生产输入回放通过。执行器、heartbeat 发射、业务清理和执行权威未修改；此事与原 SIGSEGV 不合并归因。

| 本轮实际运行 | 结果 | 范围与限制 |
|---|---|---|
| fatal-outlet-selftest | rc=-11，fatal 栈持久保存 | 独立合成自发信号，只验证出口 |
| diagnostic-run-1 / 2 | 分别 48 passed，rc=0 | 原 capture，安装前串行；真实根因 UNKNOWN |
| 第三次诊断 | NOT RUN | 没有新证据，按上限内停止 |
| 离线 wheel dry-run / install | dry-run rc=0 / install rc=1 | 仅两包计划；只读导致安装失败，无环境差异 |
| owner-not-deployed | passed=false，实际 PlainTerminal | 未安装时的新进程真实来源负证据 |
| 初始样式驱动两个回放 | 均 -9 超时 | 未接既有 pulse；保留失败，不作原生崩溃归因 |
| 修正驱动后的 baseline / candidate 正常、错误 | 均 PASS | 同一驱动、生产 Terminal；依赖来自隔离缓存 |
| style-unit-cache | 30 passed，rc=0 | 三个显示测试文件，原默认 capture |
| 宽 42 长审批+详情 / 空白粘贴 | PASS / PASS | 内核 PTY，pending 0，明确批准后执行 1；缓存依赖 |
| 无颜色 / 显式缺包 | PASS / PASS | 无颜色仅 reset/bold；缺包仍降级 |
| 登录终端 dry-run / install | 均 rc=0 | 只新增两包，无移除；原失败回执仍保留 |
| 实际安装逐文件核验 | DEPLOYED | 目标绝对解释器、实际 site-packages、无缓存 |
| 安装后 owner 矩阵 | 8/8 PASS | 动态三例、fallback 五例；真实内核 PTY/pipe |
| 安装后 baseline 正常/错误回放 | 2/2 PASS | 与候选使用同一实际依赖，调用/动作不变 |
| installed-tests | 73 passed，rc=0 | 六个显示/审批/输入测试文件，无缓存、原 capture |
| 首次 installed-approval-off-approve | child rc=0；外层断言失败 | 合法 HEARTBEAT 被旧测试误拒绝；保留失败 |
| installed-receipt-tests | 12 passed，rc=0 | 修正测试后验证严格完整生命周期及无效回执 |
| 最终安装后审批/输入 PTY | 9/9 PASS | pending 0；批准后 1；拒绝/反馈/EOF/取消 0 |

证据文件路径、字节数和 SHA256 见 [evidence-manifest.json](evidence-manifest.json)，每次的 invocation/result/receipt/pytest-results 保存实际命令、退出码与阶段。仅复制新建隔离子进程的明确产物；不收录 scratch 数据库、完整业务状态或 wheel 二进制。fatal 原运行文件保持 0600；入 Git 的脱敏副本为普通文档文件。

**验收命令与保护范围**

```bash
python tests/export_cli_deployment_fixture.py /tmp/NEW-export --tree <本轮已验收树>
python tests/cli_v2_support.py --export /tmp/NEW-export --results /tmp/NEW-tests --installed-cli -q tests/test_cli_semantic_styles.py tests/test_cli_v2_display.py tests/test_cli_v2_terminal_fallback.py tests/test_input_boundary.py tests/test_approval_visibility.py tests/test_approval_owner_lifecycle.py
python tests/cli_v2_support.py --export /tmp/NEW-export --results /tmp/NEW-receipt-tests --installed-cli -q tests/test_cli_deployment_receipts.py
PYTHONPATH=. python tests/replay_approval_visibility_pty.py --export /tmp/NEW-export --results /tmp/NEW-details --scenario details --mode tty --cols 42 --installed-cli --assert-fixed
PYTHONPATH=. python tests/replay_input_boundary_pty.py /tmp/NEW-export /tmp/NEW-input --mode tty --decision blank-paste --installed-cli
```

生产终端验收仅表示目标环境的 CLI owner/显示/输入在这些合成场景成立，不表示旧进程已更新或真实 WGS 可运行。SIGSEGV 真实根因仍 UNKNOWN，未宣称修复；未做 GDB/完整 C 栈采集，未重构长命令续查或新增运行中模型回合。明暗主题依终端 ANSI 调色板及默认前景适配，没有真实多主题截图或字体矩阵，不宣称像素级验收。完整执行列表、命令、SHA256 与各次 rc 见证据索引；原始 core/内存和原业务数据不入 Git。全部原有 437 条工作区状态保留，指定五文件 SHA256 不变；未 push。
