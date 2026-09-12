# 审批可见性与短预检反馈交付

本轮按 2026-09-12《审批提示可见性与预检反馈》规格增量实施。基线为分支 `refactor/consultant-controller-authority` 的 `53ff7dba6716d30fd5bbe3a508fa9aef5ba0b4d3`，没有回退或重复 Plan/结果验收改造。没有 push。

长正文与并发输出现在由同一终端 owner 协调审批呈现和输入。compact 保留摘要及完整详情入口；TTY 固定待决状态，plain/off 仍先呈现必要审批内容再接收决定。原 Enter/y、拒绝、反馈和退出语义保留；legacy 非 SAFE 命令仍不能空回车批准。详情、EOF、取消、超时、旧回调、正文替换或显示故障均不能批准当前候选。

新预检封装逐项报告开始、真正 spawn、返回、超时及取消，保存子项完整 stdout/stderr，合并一次外层正式 observation。默认局部时限为 60 秒/项，可在新候选中调整，不套用于长计算或审批等待。原批准脚本没有修改。plain 的逐项反馈也实际写出；off 隐藏普通进度但不会绕过人工审批。

## 提交与文件范围

| 切片 | 完整 commit | parent | 文件范围 |
|---|---|---|---|
| AP-1/AP-2，不可分的呈现及输入协调 | `48033960176591f94a9a9b9c1187a998b0fbe35a` | `53ff7dba6716d30fd5bbe3a508fa9aef5ba0b4d3` | 13 文件：cli_terminal、cli_plain、cli_review、cli_display 的 input_terminal、human_io、bio_agents._prompt_user、confirmation 的 legacy 显示 scope；3 个新增测试/回放入口、exporter，2 个原清洁 CLI 测试适配 |
| OBS-1，新候选短预检封装 | `132b11667930315e5f605fd08073f8f700e2b82d` | `48033960176591f94a9a9b9c1187a998b0fbe35a` | 13 文件：preflight、preflight_progress、executor opt-in；StreamDisplayCopy；node_context 及两处 Recovery 模型副本过滤；新 batch 脚本、3 份 Skill、测试及 exporter |
| OBS-1，plain 实际写出补充 | `07eae17c544479444f2dfe4e531845e3f36b2f31` | `132b11667930315e5f605fd08073f8f700e2b82d` | 3 文件：cli_display、cli_plain、test_preflight_feedback；每个真实边界写紧凑行，仍受待审批输出规则约束 |
| 本目录审计与回放证据 | 单独后续文档提交，完整 hash 见交付回执 | `07eae17c544479444f2dfe4e531845e3f36b2f31` | 仅本目录 |

精确逐文件列表由 `git show --stat <commit>` 或本目录 `validation.json` 获取。第三个提交来自归档时增加“实际 writer 已写出”的核查：首版 OBS 测试仅证明当前项进入 display model；发现 plain 缺可见输出后修复，并保留这一区别，没有把内部状态等同于终端可见。

## 实际验证

| 验收 | 隔离 Git tree | 实际结果 |
|---|---|---|
| AP 最终完整回归 | `20a037b818cfa1cdbf99977d5e898e6719e02e2d` | **83 passed / 6.64s**，SOURCE_UNCHANGED True |
| AP 内核 PTY 主矩阵 | `c6d24c71da3bbfe89f20600791fe3f613ef3d332` | **12 组通过**：代码、15 步 Plan→代码、详情、粘贴、y/q、plain/off、verbose 延迟 writer、编辑、EOF、Ctrl+C、resize |
| AP 最终树 PTY 复验 | `20a037b818cfa1cdbf99977d5e898e6719e02e2d` | 详情代表回放全部机器断言通过；最终树增加正文替换拒绝等边界，18 项生命周期测试也在 83 项内通过 |
| OBS 最终基础验收 | `f2378f450411116eab533298bd2ce5ce259789c8` | **14 passed / 4.95s**，SOURCE_UNCHANGED True |
| OBS plain 写出加强后 | `2834bd3c449c12ef621d4566f4ded0f30b79c13e` | **17 passed / 3.92s**，含重跑的 14 项 OBS 与 3 项 plain 回归，SOURCE_UNCHANGED True |

所有源文件由现有白名单 exporter 从指定 Git tree 导出，导出没有夹带原 dirty 或未跟踪文件；既有 offline guard、fake provider、新 tmp 状态和无害小进程继续生效。未恢复原 session，未调用真实模型/API、WGS，未读取原业务数据库、启动.sh、密钥或敏感配置。没有新增旁白模型调用。

AP 内核 PTY 使用生产 `review_code/PlanReview → OutputRouter → BioAnalystAgent._prompt_user → human_io → Terminal/PlainTerminal`。代码为 430 行以上合成正文，追加 500 行普通活动、迟到 provider 活动和必要错误；阻塞 writer 使用 Event 边界。真实 `run_terminal` 的 after_render 故障→plain→原 Future 的 y/n/EOF 另在生命周期测试中使用内核 PTY 验证。最后屏幕可见、未决执行 0 次、合法批准后真实无害执行恰好 1 次。

前后完整批准 payload、脚本 SHA-256、stdout/stderr、返回码、模型调用数和路由逐项相等。相等的脚本 SHA-256 为 `eba70a9e8f257d56f7bd1c70a1f7c5dc802d3b586713cbf43aa9ed0835c72fca`。执行 PID、真实单调时点自然不同，未用相同时间数值伪造等价。OBS 会改变新候选反馈和期限，未混入 AP 纯显示等价声明。

## 可复现命令

以下均从仓库根执行。目录须为新的安全隔离目录；运行器使用 `/public/home/liusiqi/anaconda3/envs/biomind/bin/python`，CLI 依赖来自既有 `/tmp/biomind-cli-deps`（prompt_toolkit 3.0.52），没有升级依赖。

```bash
python tests/export_approval_preflight_fixture.py /tmp/biomind_approval_preflight_20260912/ap-final-export --tree 20a037b818cfa1cdbf99977d5e898e6719e02e2d
/public/home/liusiqi/anaconda3/envs/biomind/bin/python tests/cli_v2_support.py --export /tmp/biomind_approval_preflight_20260912/ap-final-export --results /tmp/biomind_approval_preflight_20260912/ap-final-tests -q tests/test_approval_visibility.py tests/test_approval_owner_lifecycle.py tests/test_cli_v2_display.py tests/test_cli_v2_output.py tests/test_cli_v2_plain_stage.py tests/test_cli_v2_terminal_fallback.py
/public/home/liusiqi/anaconda3/envs/biomind/bin/python tests/replay_approval_visibility_pty.py --export /tmp/biomind_approval_preflight_20260912/ap-final-export --results /tmp/biomind_approval_preflight_20260912/ap-final-tty-details --mode tty --scenario details --assert-fixed
python tests/export_preflight_feedback_fixture.py /tmp/biomind_approval_preflight_20260912/obs-plain-final-export --tree 2834bd3c449c12ef621d4566f4ded0f30b79c13e
/public/home/liusiqi/anaconda3/envs/biomind/bin/python tests/cli_v2_support.py --export /tmp/biomind_approval_preflight_20260912/obs-plain-final-export --results /tmp/biomind_approval_preflight_20260912/obs-plain-final-tests -q tests/test_preflight_feedback.py tests/test_cli_v2_plain_stage.py
```

以上记录的是实际使用的目录，重复时应另取新目录；所有原始命令也保存在 invocation.json 及 approval_evidence.json 的 replay_command 中。

## 证据入口与限制

- `fault_chain.md`：一页故障链与最小函数修改。历史 356 秒属于审批等待；原运行缺呈现完成时点，不能全部称为用户阅读，也不能归因于 FASTQ 死锁。
- `approval_matrix.tsv` / `approval_evidence.json`：A01–A13、前后对象等价、单调事件与实际 spawn、运行命令及开发失败历史。
- `obs_matrix.tsv` / `obs_evidence.json`：O01–O08、原始双流保存、timeout、取消和前缀证据范围。
- `pty/`：修改前后生产 renderer 的合成终端 ANSI、最后屏幕及机器断言；全部按键来自 harness，不是原事故录像，不证明真人已阅读。`artifact_manifest.json` 给出文件哈希。
- `workspace_protection.json`：原 437 条 dirty/untracked 状态逐项保留；原 node_context 修改未进入提交，正式 prompts/manager.py 与 middleware 哈希未变。状态相同不冒称每个原 untracked 文件都做过字节级复核。

已保留的未通过记录：首轮旧 CLI 断言 **12 failed / 53 passed**，其中默认重复全文、绕过渲染握手及 pending 后换 context 的旧预期按新规格改正，随后完整 83 项通过；开发版 plain 曾丢弃必要通知后的合法 Enter，已修复；详情 harness 曾依据旧帧重复注入 Enter，改用真实 submit 与新帧屏障。早期生命周期测试 4 次在 asyncio 默认 executor 退出回调处超时，保留堆栈，复用既有 callback servicing 测试 fixture 后完成；另一轮 15 passed / 2 failed 是跨参数 AppSession 缓存已关闭 PTY，改为每例独立 AppSession。未通过运行未计入通过数。

旧 progress/display 组合在旧 HEAD 和当前候选同为 **20 passed / 1 failed**：ContextVar 外层 `flush_public(timeout=0)` 断言失败，本轮不修。历史 SIGSEGV 没有宣称解决。普通 pytest 保持原捕获设置；PTY 回放从基线到新版均使用暴露终端所需的 `-s`，没有用它把历史捕获崩溃改称通过。

完整 managed 编辑→Generate 业务链未重跑，回放验证的是新审批对象、旧 Future 隔离和原路由未改。宿主 TTY 所有物理 type-ahead 时刻不能识别；多行粘贴和程序回调归属已检验。无独立 owner 的回放验证有序输出，持续并发输出由实际 TTY/plain/off owner 回放覆盖。永久阻塞 OS write、不可中断 I/O 或逃离管理进程组的后代不能承诺回收；审批展示握手有界失败且不会批准，但不可返回的旧 writer 清理必须记录为未确认。

实现核对使用当前安装的 prompt_toolkit，API 语义参考其[官方文档](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/reference.html)；事故结论和验收均来自本仓库静态证据与本轮离线运行。
