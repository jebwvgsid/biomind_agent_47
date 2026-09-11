# BioMind CLI 定点实施与离线验收

实施日期：2026-09-11。实现与测试已提交；**整体规格尚有未完成项，不宣称全部验收完成**。

## 基线与提交

- BASE HEAD：`94f8eccb65a9a68cf5a32e266b8dda51e71390e7`
- BRANCH：`refactor/consultant-controller-authority`
- CLI-0：`948790396bba8d1af1d851d4b163c519dae8d95c`
- CLI-1 显示通知/输出副本切片：`4a0b51121c5bc776bc90c4366ee6d73d0206e87f`
- CLI-1/2 真实入口、终端所有者与交互：`08ecb7361a4514f4415455b04500d0879b6d4de7`
- 补充粘贴、异常退出、trace 验收：`0b490cd3d2d4daf1032499258a7afc8080bd147f`

报告及录制另作文档提交。未 push。原有 104 个 porcelain 状态项保持；原有 61 个 tracked diff 在忽略因新基线产生的 index/hash/hunk 坐标后逐项一致；原 staged 为空，代码提交后仍为空。`config/logger.py` 原有 `BIOMIND_DISABLE_DOTENV` 改动通过只暂存新 hunk 保留，未混入提交。未改动原有输出测试。

## 实际根因

| producer | 原接缝 | 新目的地 |
|---|---|---|
| BioMind.Performance | INFO 传播到 root console + file | 文件保留；终端不显示逐 span |
| BioMind.Agent 的 prompt accounting、内部记录 | 同一 INFO console | 文件；警告/错误由 owner 摘要展示 |
| agent_core.provider_attempt | INFO 大 JSON，同时已有 callback | JSON 仍走原日志；已有事件投影有限活动/取消事实 |
| typed_node、httpx | INFO 传播到 root | 文件；HTTP 200 不构成完成 |
| VibeThinkingHandler | 直接打印 reasoning 片段 | 只显示收到回复活动；不输出内部文本 |
| OutputRouter、legacy print、memory 状态 | 终端直接打印、pager、节点横幅 | 显式 display_print/emit；memory 诊断进日志；正常交互时只有 owner 绘制 |
| executor pipe | 原始捕获、原日志与 parser 路径 | 这些路径保持；另取增量解码显示副本 |

CONFIRMED：原 console handler 接收 INFO；`basicConfig(force=True)` 清理整个 root；provider callback 绕过输出路由；原 POSIX TTY 输入同步占用 runtime loop。附件中的刷屏不需要用“重复 handler”解释。重复注册是修复后的幂等验收项，未声称原运行存在重复 handler。

UNKNOWN：未读取原 session、生产日志、进程环境凭据，因此不推断历史运行最终状态、旧终端是否已安全停止、实际启动依赖/外部 handler 配置。

## 实现及边界

CLI-0 完成：只替换 BioMind 自有 handler，不清空外部 handler，不 disable logging；相同配置重复初始化不增加 handler。文件路径在进程内稳定。文件失败只提示一次，警告/错误仍可见；完整 traceback 保留文件。DEBUG 只扩展文件诊断。没有新增性能审计链或 fsync。

CLI-1/2 已接入 `bio_agents.py:main → run_terminal → _runtime_main → interactive_mode`。Python `prompt_toolkit.Application` 是动态终端所有者；原 async runtime 在一个线程中保持独立事件循环，避免现有同步节点/输入阻塞 UI。没有 Rust、RPC、后台服务或另一个 Agent。原 `_prompt_user` 的等待状态持久化边界保留。

稳定消息只追加一次；活动状态独立更新；跨线程通知合并，绘制最短间隔 0.1 s，运行时秒级时间更新。历史通过 owner 的终端让渡追加，详情用受控只读视图。UI tick/render 不加载 Plan，不迁移、不写业务状态。

输入支持中文、Tab、空行、全角空格、Ctrl+J 换行、bracketed paste；/paste + /end 作为保底。多行任务不参与斜杠命令或 shell 分发，不再对多行正文 strip。采用完整文字 Buffer，不用同长度占位符关联粘贴。终端 CRLF 按输入库约定成为 LF；正文不做 Unicode 规范化。运行中草稿不提交、不排队；审批显示原 payload 的独立副本，后端仍处理响应和授权。F2/详情后 Esc 返回保留草稿，详情打开时 Enter 不会批准。

第一次 Ctrl+C 调用已有 attributed cancellation API；再次 Ctrl+C 提示未确认，Ctrl+D 明确强制离开，先恢复终端，再调用既有进程终止边界。provider 清理、取消状态保存、运行调用返回各自展示；不称整体安全停止。`rc=0` 只显示等待正式核验，最终状态来自 `InteractiveRunResult`。

普通 `!cmd` 仍调用原 subprocess 接口，通过终端让渡执行，没有新增执行器。原 stdout/stderr 捕获、完整日志、正式 parser 输入不改；显示副本隐藏 machine marker、清理 ANSI/OSC、限制尾部为 4096 字符。stdout/stderr 持续并发排空，不 await UI 绘制。

renderer 异常先恢复终端再降级 plain，继续同一 runtime，保留待输入请求，不触发恢复/重试。非 TTY interactive 入口明确返回，不弹框等待；plain 的只读命令不会送给模型。

## 验收命令与结果

工作目录：`/tmp/biomind-cli-20260911/release`。Python：`/public/home/liusiqi/anaconda3/envs/biomind/bin/python`，3.12.12；pytest 9.1.1；prompt_toolkit 3.0.52（离线缓存副本）；Linux 5.14 / Intel Xeon Gold 6430。

测试代码导出：

```bash
python3 tests/export_cli_fixture.py /tmp/biomind-cli-fixture-new
```

只复制明确白名单源码和测试；不复制 config/startup、凭据或 session。测试 guard 拒绝网络及原工作区/启动文件/环境文件读取。executor 以真实模块载入，绕过 execution 包的无关 eager imports；真实 CLI 函数和 interrupt loop 通过 AST 原样编译，注入 fake backend。**这不是全模块生产启动测试，也没有运行真实模型。**

统一环境前缀：

```bash
PYTHONPATH=/tmp/biomind-cli-deps PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1
```

以下实际运行于隔离目录（前缀加上述 Python）：

```text
timeout 45 python -m pytest -q tests/test_cli_logging.py tests/test_cli_display.py tests/test_cli_terminal.py
→ 13 passed in 3.94s

python tests/cli_pty_acceptance.py /tmp/biomind-cli-20260911/accepted-pty
→ 210 行粘贴全文一致；30 秒等待、35/80/120 列 resize、草稿、详情返回通过；取消请求计数 1；termios 恢复

python tests/cli_pty_acceptance.py /tmp/biomind-cli-20260911/equal-pastes --equal-only
→ 两段各 1000 字同长度粘贴，删除第二段，提交正文完全等于第一段；termios 恢复

python tests/cli_pty_edges.py /tmp/biomind-cli-20260911/accepted-edges-plain
→ 批准拒绝、异常摘要、延迟取消/二次提示/强制退出、真实 renderer 抛错降级、plain 命令、非 TTY 全部通过

python tests/cli_pty_edges.py /tmp/biomind-cli-20260911/fatal-exit --fatal-only
→ 未捕获 runtime SystemExit(17)，exit=17，termios 恢复

python tests/cli_pty_latency.py /tmp/biomind-cli-20260911/accepted-latency
→ 后台 576 条日志期间 32 个中文按键，无原始性能行；p95 0.812948 ms

python tests/cli_output_benchmark.py /tmp/biomind-cli-20260911/baseline-logger.py /tmp/biomind-cli-20260911/output-benchmark.json
→ 同 fixture 的 handler 输出对照见下表
```

baseline-logger.py 来自 `git show BASE_HEAD:config/logger.py`，仅编译 logging 类，未导入配置包。

开发期失败没有当作通过：PTY 启动握手/CPR 与 fake 参数缺失已修复；跨线程输入额外线程池唤醒问题通过保持原 runtime 同步输入边界解决；双管道测试的子进程 wait 增加 10 s 上限，曾挂起的测试已中断；最终所有上述验收通过。

## 原始指标

| 相同的 582 条日志 fixture | before | after |
|---|---:|---:|
| 终端 UTF-8 字节 | 57583 | 130 |
| 终端行 | 582 | 2 |
| 终端 perf 行 | 576 | 0 |
| 文件 perf 行 | 576 | 576 |
| 屏幕 warning / error | 1 / 1 | 1 / 1 |

这是日志分流结果，不是 WGS 提速。

最终 PTY 210 行场景：15181 个原始终端字节，含控制序列；588 次显示更新，88 次 after_render，7 条稳定历史。活动不会变成 576 条历史。旧模式同条件完整 TUI history/redraw 指标未测量，**UNKNOWN**，不以日志行数冒充业务历史。

中文缓冲响应：32 样本，p50 **0.153044 ms**，p95 **0.812948 ms**；18 次绘制，3 条历史，576 条后台 perf 全在文件。测量从 PTY 写入到 Buffer change 回调，不包括像素显示、SSH 网络或模型响应；旧版同口径延迟 **UNKNOWN**。

1000 次 UI status/render 回调：业务状态读取 **0**、业务写入 **0**、业务审计事件新增 **0**。正式 effect parser 的输入 SHA 和 receipt 在显示开/关后相同。双管道小子进程每个 stream 的原始字节与 fixture 完全相同；长度及 SHA 见 evidence.json。真实任务端到端读写数与收益 **UNKNOWN**。

## 回放与文件

`recordings/paste_wait_resize.cast` 包含启动、技能发现、工具执行、验证问题、最终失败结果；其它记录覆盖批准、取消、等长粘贴、异常退出和 renderer 降级。均是真实 PTY 采集的 fake 场景，非设计图；不会把 fake “7 个候选”描述为原任务事实。

```bash
python docs/implementation/cli_terminal_20260911/replay.py docs/implementation/cli_terminal_20260911/recordings/paste_wait_resize.cast --speed 4
```

可用 asciinema 播放同一 `.cast`。文件完整列表在 `changed-files.txt`；源码指纹在 `source-sha256.json`；原始指标与录制一并提交。

## 尚未完成 / 限制

1. **固定 upstream commit 的源码核对未完成**：已固定候选 `e25bedc166b54acadb505b587a387091097b2393`，实际读到的是官方动态源码，不能证明它们对应候选 SHA。详见 upstream-reference.md。CLI-1 不标作完整规格验收完成。
2. 当前 biomind Python 环境未安装动态依赖；只在 `/tmp` 离线副本验收。代码会明确降级 plain。启用动态界面需要目标运行环境安装 `requirements-cli.txt`。未擅自改动工作区外的运行环境。
3. 真实 BioAnalystAgent 构造、全部第三方 import、真实图/Provider/科学任务与 SSH 环境没有运行；验证的是原样 CLI 函数 + fake backend、真实 executor 读取函数和正式 effect parser。不能把离线通过写成原任务恢复或生产集成全面通过。
4. 内联 PTY 验证覆盖 resize、草稿和 termios；没有证明所有 SSH/终端的 scrollback 都与 Codex 等价。未实现 JSON 机器输出模式，也不宣称普遍支持 Shift+Enter。
5. 细粒度技能候选数、重试预算、各后端清理状态只在已有通知提供时展示；未新增业务查询补全。状态条显示最近观测时间，未提供完整阶段总耗时面板。plain 的 `/details` 退化为日志位置，不提供动态缓存详情。
6. 详情列表有界，审批上下文保留完整显示副本；工具屏幕尾部有界，全文仍在原日志。外部组件自有 handler 不被强制移除，未开展全仓/全依赖输出审计。

REAL MODEL/API/WGS：NOT RUN。ORIGINAL SESSION：NOT RESUMED / NOT MODIFIED。未重建索引、未实施 E2、未改变正式 parser/完成语义，未读取或执行 启动.sh、.env 或凭据文件。
