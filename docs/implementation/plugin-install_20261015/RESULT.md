# BioMind 插件安装体验实施结果（2026-10-15）

开发候选已实现日常一条命令安装，适用于新建隔离目录或已合法接管且无冲突占用的资源目录。当前生产尚未启用这些改动：PATH 仍指向旧固定 release，实际用户目录 legacyUnverified=true。适配包本地可安装，远程一条命令分发未发布。

本轮没有接管生产目录、切换入口、重启服务、停止用户任务、push 或 publish。所有新增会话/包写入都在隔离目录。测试运行过真实 Pi/npm/git/CLI/Studio 适配器，模型使用脚本化 provider；不是科研任务成功，也不是 Windows 实测。

## 权威源码与实际入口

权威核心 `/public/home/liusiqi/03.毕设/BioMind-worktrees/unified-core-plugins-20261009`，起始分支 `prompt/common-rules-20261015`、HEAD `5fe00a1806e5c77c9c42b32d8e60aade34aa2ec2`。未静默换核心；原有 aporec-porec-contact 技能 WIP 和 evidence 未提交到本任务。隔离验收目录 `/public/home/liusiqi/03.毕设/BioMind_Plugin_Install_20261015`。

实际依赖为 `@earendil-works/pi-coding-agent`、`@earendil-works/pi-agent-core`、`@earendil-works/pi-ai`，均精确锁定 0.87.1。前者实际路径为权威核心 `integrations/pi_runtime/node_modules/@earendil-works/pi-coding-agent`。现有兼容层 `0.87.1+biomind-observer.6` 与 `plugin-guard.1` 保留；本轮没有改 Pi 版本、lockfile 或这些兼容补丁。

链路为 `bin/biomind → bio_agents.py/pi_entry → pi_plugins.py → 当前 runtime/dist/plugin-cli.js → 原生 DefaultPackageManager + SettingsManager → agentDir 或项目 .pi → DefaultResourceLoader → 会话`。不会从 PATH 随意选另一个 Pi。

生产 PATH `/public/home/liusiqi/.local/bin/biomind` 指向 `/public/home/liusiqi/03.毕设/BioMind/bin/biomind`，再固定到 `BioMind_Unified_Core_20261009/releases/biomind-30d724698f8f77ad`；releaseId `30d724698f8f77adf5b7d5b53b0d5c0de06963510b20e44398b8595794a30e8a`，sourceCommit `c53c4609961f5fca6db331e2d6fd25306799cbbb`。只读 list exit 0，packages=[]，用户 npm/git/tmp 目录 legacyUnverified=true。

生产 cli-runtime.json 与 workbench-runtime.json 的 installRoot/agentDir 相同；agentDir 为 `/public/home/liusiqi/03.毕设/Biomind_v1/integrations/pi_runtime/.biomind/pi-agent`，Python 路径属于同一 conda 环境。磁盘配置一致不证明当前 Windows 窗口连接一致：本次 Linux 主机 ln02、用户 liusiqi、uid 33068；本机 profile HTTP 返回 Authentication required，systemd 用户总线不可访问，未读取凭据以绕过。实际生产会话、项目 cwd、Windows 后端绑定仍未核验。

Studio 权威目录为 `/public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/studio`，main/`573432a7e5f7d1099aacd2b29fcb5e2385be8793`。桥接本来就用固定 server entry/config 调 `plugins --json`；14 项桥接回归通过，本轮不需要改 Studio 源码/界面。

## 上游复用与真正缺口

| 能力 | Pi 0.87.1 已有实现 | 原 BioMind 接入 | 本轮缺口与修改 |
|---|---|---|---|
| npm/git/本地、版本/commit | 原生 parser、installAndPersist/removeAndPersist/update | 已接入 | 保留；真实 npm 固定版本和 git 固定 commit 验收 |
| settings 与项目范围 | SettingsManager、项目 trust、原生持久化 | 已接入、带 revision 与锁 | 保留，不自动信任，不改无关字段 |
| 资源过滤和去重 | manifest/filter/autoload delta、project wins、原生 identity | list 只给配置行 | 用固定版本原生只读方法计算资源数/禁用/覆盖；没有第二套规则 |
| 静态清单 | listConfiguredPackages/getInstalledPath | 已有 | 避免公共 resolve 的自动安装/祖先扫描；查询期间禁用会执行 npm root -g 的 legacy fallback，不借另一全局安装冒充当前范围 |
| 进度 | setProgressCallback/ProgressEvent | 未传到终端 | 映射中文阶段、真实耗时，无百分比猜测 |
| stdout 协议 | 原生包子进程输出 | 独立结果 FD，默认 JSON | 保留结果 FD，默认文本，脚本显式 --json；有界脱敏日志/verbose |
| 加载/重载 | DefaultResourceLoader、session reload、runtime replacement | BioMind 原生和 Studio host 已有 | 复用；安装不主动重载，静态 list 不声称会话 loaded |
| 并发/失败 | settings 保存非全事务 | 已有 flock、adoption、repair 标记 | 全部保留；增加文件/配置阶段、阻塞解释、取消和超时结果 |
| 宿主更新 | 原生 CLI update 与 --extensions 不同 | 已拒绝宿主 update | 保留拒绝入口与测试；调用 package manager 的 update，不调用 Pi CLI 自更新 |
| output-limits | 上游原版默认原生上限 | 已有必要 BioMind 适配，入口在包外 | 独立名称/版本、包内 bundle、适配默认8192/300、冲突检查；原版不替换 |

私有 API 依赖集中在 plugin-inventory.ts：Pi 未导出“不安装、不自动发现祖先资源”的完整静态资源清单 API，因此复用固定 0.87.1 的 identity/dedupe/collector 方法，并对方法存在性及真实加载器结果作回归。没有复制来源解析器或资源优先级；升级 Pi 时必须复验该适配点。

安装的真实顺序是先修改包目录，再 addSourceToSettings，最后 flush/检查错误并核对路径；AndPersist 不是全量事务。失败可留下已安装文件和安装脚本副作用，repair marker 阻止后续误报健康。本轮没有建立新的包库、商店或事务系统。

## 实际修改

- Python 入口默认中文目标、阶段、包/配置结果；新增 --verbose。结果仍使用 BioMindPluginsV1，--json stdout 无进度。日志 0600、1 MiB 上限，长行丢弃而持续排空，不无限累积 npm 输出。900 秒超时或用户取消只结束本次新建进程组，保留未知结果/修复提示。
- 机器回执补充 hostname/uid/user/platform/runtime/settingsPaths、实际版本/commit、文件阶段/配置阶段、资源数/enablement、sessionObservation（unknown/null）及功能未核验。静态 list 不联网，不执行扩展，不启动模型。
- 项目首次安装也先检查明确授权；空目录资源读取安全不作为写入插件的授权，避免写入首个包后信任状态改变造成半完成。
- 旧目录与 BUSY 分别解释；守卫和一次性接管流程未删除。日常写操作仅争用实际资源目录的锁，已接管目录不重复人工接管。共享 npm 依赖可能变化，因此保持 worker 同资源范围读取锁；服务进程单纯存活不是全局禁装条件。Studio 原有协调只接受所有关联 owner 的空闲回执，活动计算不会被停；本轮未改该流程。
- 将原有依赖默认 JSON 的 Python 测试改为显式 --json。Studio 已显式声明，直接调用 managePlugins/plugin-cli 内部 IPC 的脚本不属于终端默认输出消费者。外部脚本迁移规则见 OPERATIONS.md。
- `biomind-pi-output-limits@0.1.0-biomind.1` 保留原 BioMind 结构化适配，新增包内入口和 8192 字节/300 行默认值，显式用户/可信项目配置仍覆盖。原版/适配版冲突不静默替换；已知安装冲突在包写入前拒绝，config 冲突在设置写入前拒绝；远程元数据事后才可见的冲突保留 repair 标记。原生过滤可显式禁用其中一个。
- 实际 npm pack 后解包到独立目录安装，包只依赖 Pi 宿主 API/Node 内建模块，无另一份 Pi、包外开发路径或修改核心的 install hook。upstream-lock 与补丁可从已存档上游重建。

## 生效与验收边界

安装仅修改配置/包，不证明运行中会话加载。新进程、Studio 新 worker 和从历史恢复 worker 会建立资源加载器；原生 /reload 与 Studio reload 走已有加载器重建。真实 CLI 的同进程 /new 另有本轮生命周期证据，见验收表/cli-session-lifecycle.jsonl；刷新网页没有此保证。静态 list 的会话/功能始终 unknown，不能缓存旧进程 loaded 标记。

Pi-output-limits 的普通文本**正文**预算是 8192 字节/300 行，退出状态、截断说明、续读引用在正文之外；结构化结果仍沿用字段分页。上游原版和 BioMind 适配版不是同一分发物。上游 npm 0.1.0 和 git ff06cbc30135c82f2a89a6ff1b420d6759693acd 均已真实安装，未执行未知来源扩展；原版默认不改变原生上限。适配版本地安装/运行已验证；未发布，远程一条命令分发 NOT_RUN。

## 验收矩阵

证据根：`/public/home/liusiqi/03.毕设/BioMind_Plugin_Install_20261015`。命令详细参数保存在 acceptance.json、remote/results.json 及本报告下方；无费用的 scripted provider 会捕获真实 Pi 组装后的模型 context。

| 编号 | 结果 | 实际覆盖与证据 |
|---|---|---|
| T01 | PASS | check-plugin-install-experience.py 的 install/list/remove/reinstall；真实加载另见 CLI/Studio chain |
| T02 | PASS | 本地独立包、真实 npm:pi-output-limits@0.1.0、真实 git 固定 ff06cbc；remote/results.json（均 exit 0） |
| T03 | PASS | plugin-tests-final.tap：项目缺信任拒绝、显式 trust 后并存、project wins、autoload delta；同真实加载器对照 |
| T04 | PASS | 重复声明不增加；指定 update/--extensions 不改宿主哈希；原生 fixed pin 行为与 self-update 拒绝 |
| T05 | PASS | python-entry-final.log、studio-json-tests.tap（14/14）、cli-acceptance/json-list.stdout；显式 JSON 不含 npm 进度 |
| T06 | PASS | human-install.stdout 真实 target/start/complete/flush，无 ANSI/回车；有界脱敏日志测试 |
| T07 | PASS | 缺失、禁用、覆盖、加载错误；静态 list 不导入抛错扩展，真实 loader 报错；会话仍 unknown |
| T08 | PASS | 缺失 npm + 不可执行 npmCommand 静态查询、无扩展导入；不调用 resolve/模型/网络 |
| T09 | PASS | Studio 新 worker、reload、resume；原生 CLI startup/reload/new 见 lifecycle 证据；不自动 reload 用户会话 |
| T10 | PASS | plugin-tool-policy：Main 实际执行 fixture_echo 并送入下次 provider 请求；仅钩子包无工具但加载成功；protected 模式按原策略拒绝 |
| T11 | PASS | Python guard/adoption 与 native manager 隔离接管、幂等、共享/独占实际 flock；接管主机收集为受控夹具，非生产接管 |
| T12 | PASS | 真 npm 配置写失败、故意连接拒绝、独占并发、安装父进程中断/子进程持锁、显式 repair；wrapper 超时仅清理自有组 |
| T13 | PASS | remove 后原始本地 package/source 和无关 settings 字段保留，无会话/记忆删除 |
| T14 | PASS | cli-acceptance/pack.json、tgz、独立解包安装与实际加载，冲突/过滤回归；远程适配版发布 NOT_RUN |
| T15 | PASS | output-limits 实际 Pi 工具/最终请求组装测试、CLI/Studio scripted-provider trace；字节/行/引用/结构化字段均核验，非真实科学任务 |
| T16 | PASS / BLOCKED / NOT_RUN | 隔离真实 Studio adapter/core 同绑定 PASS；生产 HTTP 认证/用户总线限制 BLOCKED；Windows 实机与真实生产会话 NOT_RUN |

主要可复跑命令（从权威核心执行，NODE 为现有 v22.22.0 二进制，不安装/升级 Node）：

```sh
python3 -B -m unittest discover -s tests -p 'test_pi_plugin*.py'
"$NODE" --max-old-space-size=1024 integrations/pi_runtime/node_modules/typescript/bin/tsc -p integrations/pi_runtime/tsconfig.json
# 在 integrations/pi_runtime 下：
"$NODE" build-native-extension.mjs
"$NODE" build-manifest.mjs
"$NODE" --test --experimental-test-isolation=none --test-concurrency=1 dist/tests/plugin-install-experience.test.js dist/tests/plugin-management.test.js dist/tests/plugin-tool-policy.test.js dist/tests/output-limits.test.js
# 回到核心根：
python3 -B scripts/check-plugin-install-experience.py --root <不存在的隔离目录> --node "$NODE" --python <现有Python绝对路径>
python3 -B scripts/check-output-limits-cli.py <上述隔离目录> <证据目录> --node "$NODE"
"$NODE" scripts/check-output-limits-studio.mjs <上述隔离目录> /public/home/liusiqi/03.毕设/BioMind_Unified_Core_20261009/studio/backend/pi-studio <证据目录>
"$NODE" scripts/check-plugin-cancellation.mjs
```

构建 512 MiB 堆不足，1024 MiB 的项目原上限构建通过。早期设置 PI_OFFLINE 导致“应阻止自动安装”夹具跳过待测路径，已移除该测试环境误设并重跑；手工创建项目夹具触发了真实旧目录守卫，已改为走原 guard 初始化后验证信任。初次 TUI reload 测试在输入框重建前输入导致超时，修正等待真实 TUI ready 后复验。保留早期失败日志，不能当作最终通过证据。

实测指标：Studio 字节夹具正文 8109 字节/94 行，行数夹具正文 2109 字节/300 行；新会话与恢复后保持一致。CLI 最终工具消息总长 8770 字节（包含预算外状态/引用/截断说明），不能误报为“整个消息小于8192”。原生启动、/reload、同进程 /new 共有6次脚本化 provider 请求，session_start 明确记录 startup/reload/new，/new 更换 sessionId。Python 专项51/51、插件专项24/24、输出限制专项12/12、Studio JSON桥接14/14。

Studio 扩展验收早期出现过子进程提前退出（未获得可归因错误文本），单独复验取得实际 ready/reload_result/工具结果/正常 exit 0，覆盖了300行夹具。本报告保留此环境不稳定记录；不将先前旧 verdict 文件当作最新验收，最终 delivery 以干净候选证据为准。

## 交付及未做事项

本任务文件以本地提交为界；最终提交/干净构建/包 SHA256 与 WIP 哈希核对保存在证据目录 delivery.json。原 skills WIP 和 evidence 保持原样。下一步只执行 ROLLOUT.md 中的获准维护交付；本轮没有自动上线。公开上游对照：[packages](https://raw.githubusercontent.com/earendil-works/pi/v0.87.1/packages/coding-agent/docs/packages.md)、[package-manager](https://raw.githubusercontent.com/earendil-works/pi/v0.87.1/packages/coding-agent/src/core/package-manager.ts)、[CLI](https://raw.githubusercontent.com/earendil-works/pi/v0.87.1/packages/coding-agent/docs/cli.md)、[扩展](https://raw.githubusercontent.com/earendil-works/pi/v0.87.1/packages/coding-agent/docs/extensions.md)、[output-limits](https://raw.githubusercontent.com/kmike/pi-output-limits/main/README.md)；实施以随包锁定代码与 upstream-lock 为准。
