# 插件日常操作（开发候选）

适用：本任务本地提交构建的候选。当前 PATH 的 `biomind` 仍是旧生产 release，尚未切换。不要把隔离验收目录当成生产 agentDir。

先确认命令打印的主机、用户、runtime、agentDir、项目和 settings 路径。Windows 连接 Linux Studio 时，应在对应 Linux 后端安装；在 Windows 本机运行不会使 Linux 会话自动获得插件。不要用 sudo。

## 正常命令

```sh
biomind install npm:pi-output-limits@0.1.0
biomind install /stable/path/to/package
biomind install /stable/path/to/package --local --project-dir /authorized/project
biomind list
biomind remove /stable/path/to/package
biomind update --extensions
biomind update npm:pi-output-limits
```

这些命令中的 biomind 必须是已核实的候选/已激活 release 入口。用户级包声明写入该 runtime 的 agentDir/settings.json；npm/git 文件落在该 agentDir 下；本地路径只登记引用，原目录必须继续存在。项目级写入项目 .pi/settings.json 和 .pi/npm 或 .pi/git，遵从既有信任；没有信任则拒绝，不自动授权。

`update --extensions` 更新配置内插件；指定 source 更新匹配的插件。无参数 update、--self、--all、self/pi 被拒绝，宿主升级使用独立 release 流程。

## 机器调用迁移

任何解析 stdout 的调用方都必须加 `--json`：`biomind list --json`、`biomind install SOURCE --json`。协议仍是 BioMindPluginsV1，新增字段兼容旧读取方。TTY 与非 TTY 都默认人类文本；不按管道悄悄改变协议。Studio 既有桥接已经使用 `plugins --json`，本任务迁移了原默认 JSON 测试消费者；直接调用 managePlugins 或 plugin-cli 内部 IPC 的脚本维持结构化 API。

成功 exit 0，占用 exit 3，失败/阻塞 exit 2，用户取消 exit 130。进度与 npm/git 输出不会混入 JSON stdout。`--verbose` 显示脱敏诊断；JSON 模式只保存诊断。日志为私有临时文件，最多 1 MiB，超长单行省略；无详细百分比。900 秒超时或取消只结束本次启动的进程组，保留不确定操作的修复标记，不声称完整回滚。

## 如何判断生效

- 已安装：文件路径存在且声明已保存。已配置但缺失会明确显示。
- 已启用：用 Pi 原生资源过滤/身份去重计算的静态选择；被项目版本覆盖会显示覆盖。
- 会话已加载：只能以实际会话诊断为据；list 始终显示未核验，带空 sessionId/observedAt，不复制旧 loaded 结论。
- 功能已验证：必须执行对应工具/事件链路测试。零新增工具的钩子插件仍可成功加载。

新会话进程重新加载同一绑定的包。已有会话可在空闲时使用原生 /reload 或 Studio 后端 reload 接口；安装本身不重载会话。刷新网页不等于重建后端。具体 /new 验收见 RESULT.md；不要推广为其他版本/入口的保证。无需默认重启整个 Studio 服务。

## pi-output-limits 两种来源

上游 `npm:pi-output-limits@0.1.0` 保持原样，默认不是 8192/300。需要 BioMind 的结构化分页、引用保护和状态保留时，使用本任务独立分发物 `biomind-pi-output-limits@0.1.0-biomind.1`（仅本地包，未发布）。解包后在稳定目录执行一次 install；包内已有入口，默认 8192 **字节** / 300 行，显式用户与可信项目配置优先。

预算沿用既定契约：普通文本正文受限，退出状态、截断说明和续读引用另行保留；结构化字段有各自分页预算。不是 8192 字符或 tokens，也不是整个模型请求总长度。插件不安装或修改核心配套；所需 BioMind 基线见包内 BIOMIND.md。

启用原版与适配版会冲突。先显式禁用其中一个的 extensions（`config SOURCE --declaration '{"source":"SOURCE","extensions":[]}'`，保留原有其他过滤字段）或 remove 其引用，再安装另一个。不静默替换来源、删除包或双重注册。

## 错误恢复

LEGACY_OCCUPANCY_UNVERIFIED 是旧目录一次性接管，按 `scripts/plugin-maintenance.md` 使用现有 audit/fence/adopt/status。实际维护计划必须审核主机、入口、任务与目录；本任务没有运行生产接管。

BUSY 表示该资源的读取者或安装写入者持锁，具体会话未查询。等待任务结束并正常关闭相关会话进程；不要删锁或 kill 用户任务。普通 Studio 服务存活不等于必然 BUSY。

repair_required 表示包文件/安装脚本可能部分完成。先看日志和配置，确认原因后使用既有机器修复入口，例如 `biomind plugins --json`，stdin 为 `{"action":"repair","scope":"user","source":"原来源"}`，并传同一 --pi-config/--project-dir。JSON 恢复不代表第三方安装脚本副作用全量回滚。接管事务的 repair 是另一流程，不能混用。

第三方扩展会执行代码；只安装信任的来源。隔离 agentDir 是数据隔离，不是操作系统安全沙箱。
