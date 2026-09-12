本轮执行动态 CLI 部署、生产样式验收和持久 fatal 取证，基于实际 HEAD 增量工作。执行跨 2026-09-12／13，沿用用户执行单日期命名。原任务、真实 BioMind 模型/API、WGS、原业务数据库、启动脚本与敏感配置均 NOT RUN / NOT READ。

**当前阶段：DEPLOYMENT BLOCKED / VISUAL LIMIT / CRASH NOT REPRODUCED。** wheel 下载和校验已完成；工具侧目标 conda 目录只读，登录终端安装回执尚未收到。已请用户执行下述安装命令。真实环境部署、无缓存动态 owner 和安装后的审批/输入矩阵尚未验收。样式候选保留在工作区，暂不作为已完成部署的交付提交。本报告提交固化已完成的诊断与有限验收证据，不代表整轮完成。

**来源与提交范围**

开始 HEAD / 上轮实际审计 commit：`86836a2ce1fd2e3d2870a01547384ffb6cad72fd`（78 个 runtime_consistency 审计文件）；其 tree 为 `89964abed21ee3ff2ce681474f279b58452ece12`。35781fd 是上轮基线，不是本轮开始 HEAD。分支为 `refactor/consultant-controller-authority`；开始 index 为空，437 条原工作区状态。

五个既有 dirty 文件保留：`prompts/manager.py`、`agent_core/planner_context.py`、`node_context.py`、`provider_message_safety.py`、`workflow/managed_stop.py`。本轮既不收编这些文件，也不恢复旧全文扫描。隔离验收采用 Git 提交树及本轮明确文件覆盖，不声明整个 dirty 工作区已验收。长命令执行器、模型回合、预算、Plan 与 typed routing 没有改动。

DIAG 测试能力提交：`4983ed592f99386b9b39301ae11b132fe070490b`，仅 5 个 tests 文件。样式/对应测试及最终文档提交见最终回执与本目录提交记录；依赖部署有独立回执，不用 Git commit 表示已安装。

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

工具端 dry-run 成功且环境差异为空；工具端安装遇到 conda 目录只读（Errno 30），rc=1、changed={}、removed=[]，失败回执保留。这是文件系统只读错误，不是自动审批拒绝。已请用户在登录终端执行相同已核验脚本，并将真实回执写入 wheel 目录下的 deployment 子目录；目前该回执尚不存在。最终是否 DEPLOYED，以实际包路径、版本和逐 wheel 文件字节核验及独立回执为准。

登录终端待执行命令，依次执行；若第一步失败，保留输出并停止第二步：

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

默认 dependency-source=installed；子进程没有临时 PYTHONPATH，记录包实际 __file__、版本及 owner。`missing` 用例显式阻断可选包来保留原缺依赖负例，安装后不会改掉该负例含义。plain、pipe、dumb、off 分别验收。初始 `owner-not-deployed` 明确记录期望 Terminal、实际 PlainTerminal、passed=false，不改写为通过。

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

已有测试的旧颜色断言按新样式更新，保留 flush、返回值、内容与审批断言。新测试核对公开完成声明不会变成工具成功、长错误正文中性色、路径样式和 ANSI 清理。当前缓存下的真实审批路径复用 HITLMiddleware → _prompt_user → owner → 原 parser → 无害进程，pending 执行 0 次，明确批准后 1 次；详情返回和空白粘贴不误批准。安装后的 EOF、取消、独立反馈及 plain/off 输入矩阵仍待运行，不借历史回执填成本轮通过。固定响应、动作和批准对象的前后比较见验收证据；样式新增模型调用=0。

临时缓存下的初始 VIS 回放只用于定位与样式对照，明确 `deployment_evidence=false`。带 `--installed-cli` 的正例和审批/输入回放待安装成功后运行，不用缓存回放冒充真实环境安装成功。

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
| 实际安装逐文件核验、无缓存 owner/审批矩阵 | NOT RUN | 等待登录终端安装 |

证据文件路径、字节数和 SHA256 见 [evidence-manifest.json](evidence-manifest.json)，每次的 invocation/result/receipt/pytest-results 保存实际命令、退出码与阶段。仅复制新建隔离子进程的明确产物；不收录 scratch 数据库、完整业务状态或 wheel 二进制。fatal 原运行文件保持 0600；入 Git 的脱敏副本为普通文档文件。未完成项将在实际部署后更新，不能仅凭脚本存在称已运行。

**验收命令与保护范围**

```bash
python tests/export_cli_deployment_fixture.py /tmp/NEW-export --tree <本轮已验收树>
python tests/cli_v2_support.py --export /tmp/NEW-export --results /tmp/NEW-tests --installed-cli -q tests/test_cli_semantic_styles.py tests/test_cli_v2_display.py tests/test_cli_v2_terminal_fallback.py tests/test_input_boundary.py tests/test_approval_visibility.py tests/test_approval_owner_lifecycle.py
PYTHONPATH=. python tests/replay_approval_visibility_pty.py --export /tmp/NEW-export --results /tmp/NEW-details --scenario details --mode tty --cols 42 --installed-cli --assert-fixed
PYTHONPATH=. python tests/replay_input_boundary_pty.py /tmp/NEW-export /tmp/NEW-input --mode tty --decision blank-paste --installed-cli
```

生产终端验收仅表示目标环境的 CLI owner/显示/输入在这些合成场景成立，不表示旧进程已更新或真实 WGS 可运行。完整执行列表、未完成项、测试命令、SHA256 与各次 rc 见证据索引；原始 core/内存和原业务数据不入 Git。未 push。
