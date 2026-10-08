# BioMind 启动边界修复（2026-10-08）

## 审计基线

实际命令 `/public/home/liusiqi/.local/bin/biomind` 链接至本仓库
`bin/biomind`，经 `bio_agents.py → agent_core/pi_entry.py → dist/host.js`，
交互模式启动锁定 Pi 官方 CLI bundle 和 `dist/biomind-extension.ts`；
JSON 模式使用 Pi SDK 及 `dist/extensions/biomind.js`。Python 后台为
`integrations/pi_runtime/python_service.py`。`dist` 按仓库约定不提交，必须完整
`npm run build`（TypeScript + esbuild），不能仅编译 TS。

起始分支 `refactor/pi-unified-runtime`，HEAD
`4baff740d89e52f5cbe3628e40d60d932ce49a20`；原暂存区为空。
实际运行源码包含大量既有未提交改动。本次单列必要前置运行时及测试依赖，
再提交修复；无关历史源码、文档删除和研究内容保留未提交。

已安装 Node **22.22.0**、科研 Python **3.12.12**、Pi 三包及锁文件
**0.87.1**。原补丁为 `0.87.1+biomind-observer.4`，本次为 `.5`。
API 和补丁锚点均核查安装版本源码，不以公开 main 文档当作已安装能力。
未更改 `APP_NAME`、configDir、`PI_CODING_AGENT_DIR` 语义或认证布局。

## 定点证据与最小修改

| 问题 | 实际文件 / 函数 | 原因 | 复用接口 / 修改点 |
|---|---|---|---|
| HOME 启动 EOF | `command_execution._directory`、`DomainService.__init__` | HOME 项目拒绝在协议服务开始前抛出 | 提取原规则为 `project_directory.validate_project_directory`，`pi_launch_metadata validate` 共用；保留后端拒绝 |
| 项目/会话顺序 | `pi_entry.build_pi_command`、`host.main` | 非法隐式 cwd 绕过选择；部分项目 Settings 早于目标会话确定 | 现有项目选择器、`--project-dir`、来源标记；Pi `findById/listAll` 核对所属项目，冲突拒绝；受信项目 sessionDir 仅作定位回退 |
| EOF 覆盖根因 | `python_service.main`、`DomainClient` | constructor 异常只落 stderr；stdout end 先锁死 EOF | 既有 hello 的 error 帧传递原因；立即 unavailable，close/既有期限后汇总 bounded 脱敏 stderr、spawn、退出码与信号 |
| 假就绪欢迎页 | `createBioMindExtension`、`installBioMindProductUI`、`startJson` | 显示组件与真实后台状态分离 | request.status 成功且连接有效才 ready；断开撤销输入/工具；重绘不清除失败；重载窗口拒绝新 JSON 请求 |
| stdin read EIO | 安装版本 `InteractiveMode.registerSignalHandlers/uncaughtCrash` | stdout/stderr 有失效处理，stdin 无对应边界 | `.5` 限定 owned stdin error，复用 `stop`、`ui.stop`、runtime dispose，记录一次并退出；菜单复用已有 TUI stop |
| Pi 更新/退出/恢复命令 | `host.startNative`、Pi `checkForUpdate/checkForPackageUpdates` 和恢复提示 | 上游硬编码操作指令未适配 | 安装版本 `PI_SKIP_VERSION_CHECK` + BioMind 条件最小补丁；恢复命令由已落盘 sessionFile/cwd 安全引用生成 |

仅科学后台初始化完成才开放分析，不增加模型联网探测。失效连接不会自动
重发模型请求或续算；失败初始化只能通过明确 `/reload` 重试。
JSON stdout 仍为原 frame 协议。公开 BioMind CLI 支持 interactive / JSON；
未新增或声称公开 print/RPC CLI 参数。UI 层仍不接管 SDK JSON/RPC 输入。

## 操作与验证边界

合法项目：`biomind`；HOME 交互启动：`biomind` 后选择/打开/创建项目；
显式项目：`biomind --project-dir '/absolute/project'`；恢复：
`biomind --project-dir '/absolute/project' --session '/absolute/session.jsonl'`。
使用临时中文/空格路径、临时会话和合成模型验证，未请求真实模型 API。

最终构建、定向测试、PTY、提交前后清单见同级交付目录
`BioMind_Startup_Fix_20261008/evidence/` 及交付报告。隔离检出只包含初始 HEAD、
明确前置依赖、本次修复；依赖复用本机锁定 Node/Pi 并验证全部补丁 hash，
不共享 dirty 产品源码或 dist，不声称从网络重新安装依赖。

两项旧科研生命周期测试在修改前服务备份上同样失败：背压取消标记时序、
crash/reconnect 的 worker-terminal 标记。保留对照证据，不扩大本次范围改执行器。

终端注入/隔离 PTY 只证明异常处理路径，**原事故 EIO 诱因仍未确认**。
当前 TAD 会话 `01a11aa8-db1d-754d-aacb-11f8499ae20b` 仅只读确认文件存在及
最后已持久化请求为 paused；未重启/恢复此会话，未探测或更改原科学作业。
后台作业存活和可续算状态未验证，不承诺所有工作已保存或可继续计算。
