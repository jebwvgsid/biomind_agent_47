# session_1789721326 性能、路径及 CLI 修复验收

日期：2026-09-18。初始 HEAD：`40ec47e8cf5d4117e3192a1b68b35878e53d7c22`，分支 `refactor/consultant-controller-authority`。没有恢复原 session、运行正式 WGS、调用真实模型/API、安装软件或 push。

## 结果和原因

原会话已进入 Planner。原始 8 条检查共 120.997s，Planner 172.537s；最后请求以 `PROVIDER_CALLER_CANCELLED` 结束，取消时已接收 24,637 个正文字符，没有完整正式计划。原始 receipt 未拆分准备和命令，因此首条 42.248s 不能全部归因于扫描。

已复用 `/public/home/liusiqi/03.毕设/biomind_audit_exports/session_1789721326_planner_delay_analysis_20260918.md`，定点复核其引用的 session 归档和请求。首个 Planner 请求 `context_trace/planner_initial-0008-4e480018d393/request.json` 确实含用户已提交的 `2倍体`、Consultant 的确认摘要及 `confirmed_decisions.sample_ploidy=2`。没有再次要求确认。最终请求 `planner_initial-0012-5290a4da7f86/manifest.json` 为 cancelled。可确认 caller cancellation；仅凭这些证据不能确定用户通过哪个关闭按键/退出入口触发，不伪造 `/exit` 事件。

原归档 4 次已完成模型请求约 11.205s，最后取消请求约 27.626s。这些请求区间包括接收，并不等于纯首字等待；没有将未覆盖的约 12.709s 全部归为模型等待，也没有将原会话标成 API 超时、规划预算耗尽或成功完成。

扫描实际用途是**构造只读挂载前排除敏感文件名**，不是写入快照、产物核验或语义发现。删除的是每条命令都对完整项目执行这项排除扫描的调用方式；敏感路径策略和受限执行仍有效。

## 实际改动

- `CommandExecutionService._argv` 默认仅展示 cwd 的当前直接条目、TaskSpec 已有类型化文件路径及本任务检查日志。目录占位不递归挂载。`read_paths` 明确选择所需文件/子树，只有被选中的子树执行递归敏感文件名排除。未挂载不被解释为缺失。
- 静态系统挂载和固定环境配置在服务实例复用。PATH、CONDA_EXE、CONDA_PREFIX、JAVA_HOME 或 conda 公开环境注册文件身份变化会重新发现环境；范围、deny_paths、软链接目标、当前目录条目和私密名称在使用时核对。没有缓存目录树或动态文件存在性，也不为缓存失效计算全树摘要。
- 保留 bubblewrap 只读挂载、私有临时目录、网络 seccomp、进程组清理和显式路径准入；不解析任意 Shell 的“只读意图”。运行环境的根级敏感名称排除沿用既有规则。准备阶段现在也计入总 timeout，可协作取消；失败/取消写独立回执。
- 已知路径通过 `inspect_paths` 在一次准备下去重批量 stat，最多 2 项并行，逐项报告 observed/missing/unverified；该固定操作无 shell、无内容读取、无写入，执行相同路径范围/敏感路径核对。任意 shell 工具仍按序执行。跨阶段复用带主机、项目、时间和截断说明的历史回执，不声称历史事实永久有效。
- TaskSpec 相对路径只按当前任务绑定的项目根解析；已有绝对路径保留，不使用安装 cwd 的同名文件兜底，也不统一 resolve 改变 wrapper 调用路径。Consultant 直接使用 typed input；正式候选搜索停止向其他项目父目录扩散，缺失显式路径不再被同 basename 文件掩盖。
- 普通 Consult/Planner、Planner repair、Generate、Recovery 和 Supervisor 使用同一检查上下文/回执入口；路径通过现有 TaskSpec/正式计划传递，不新建 registry。未来输出仍是声明，不要求提前存在。已有 Execute/受控 helper/批准与正式产物核验机制未更换，资源 ID 和普通参数不被改成路径。真实模型是否生成完整且可接纳的绝对路径计划，本次没有验证。
- compact 使用真实事件更新准备检查、执行检查、等待模型、接收实际正文字符、完整响应待解析校验、截断/中断/取消。只有真实已知的数量才显示；不显示虚构百分比、token 或 ETA。只有 reasoning/工具片段时明确暂无正文。F2 保留明细；plain 按阶段转换输出，避免逐片段刷屏。显示失败不成为业务准入条件。
- 已提交输入与新草稿分别提示。检查耗时进入既有 profiler 的 `inspection` 事件，正式 Commands 计数含义不变；计时、活动 ID、回执 ID 变化不构成 Recovery 新进展。请求超时、规划/输出预算未增加。

## 同组 8 条检查前后对照

测量主机 ln02；原会话主机 ln01。本表为当前主机顺序执行相同 8 条归档 shell 命令，修复后增加明确 `read_paths`；未并行这 8 条命令。使用真实只读检查入口，未运行科学分析。系统缓存未清理、冷热不受控，after 目录已有此前测量回执，不能把加速比当成普适承诺。

单位秒。每格“前 / 后”；扫描次数记为“递归根次数 + 浅层根次数”，是 `_private_paths` 的根枚举次数，并非所有 scandir 系统调用总数。**扫描包含在准备中，spawn 包含在命令中**；命令计时包括进程启动、sandbox 启动及回收，不能称为独立 shell CPU 时间。旧实现没有可靠的单独 spawn/首字准备分项。stderr 均为 0 字节；部分软件错误被原命令重定向到 stdout。

| 检查 | 准备 | 其中扫描 | 命令与进程 | 扫描次数 | stdout 字节 | 退出码 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 列既有输出和父目录 | 7.627 / 0.092 | 7.420 / 0.017 | 0.568 / 0.060 | 2+38 / 1+22 | 534 / 534 | 0 / 0 |
| 2 列输入目录 | 7.306 / 0.065 | 7.193 / 0.010 | 0.567 / 0.040 | 2+38 / 1+22 | 1162 / 1162 | 0 / 0 |
| 3 读旧报告/阶段状态/日志目录 | 7.978 / 0.065 | 7.867 / 0.012 | 0.649 / 0.081 | 2+38 / 1+22 | 5890 / 5890 | 0 / 0 |
| 4 定位环境与软件 | 7.956 / 0.045 | 7.841 / 0.007 | 0.580 / 0.060 | 2+38 / 0+16 | 157 / 157 | 1 / 1 |
| 5 抽读 FASTQ/reference | 8.073 / 0.066 | 7.964 / 0.010 | 1.623 / 0.362 | 2+38 / 1+21 | 1311 / 1311 | 0 / 0 |
| 6 样本表/索引/参考摘要 | 8.146 / 0.066 | 8.030 / 0.010 | 0.972 / 0.221 | 2+38 / 1+21 | 264 / 264 | 0 / 0 |
| 7 GATK/Java/bcftools/samtools 探测 | 7.857 / 0.046 | 7.744 / 0.006 | 4.148 / 1.547 | 2+38 / 0+17 | 889 / 889 | 0 / 0 |
| 8 BWA/samtools/Picard/Java/Python 探测 | 8.182 / 0.046 | 8.067 / 0.006 | 4.491 / 2.995 | 2+38 / 0+17 | 664 / 664 | 0 / 0 |

正常完成路径的对照表采集后，最终代码额外补充中断扫描计时的 finally 记录，不改变扫描范围。随附测量脚本 after 模式再次跑通 8 条命令：6.051s，整个项目递归 0 次，退出码不变。

合计：准备 **63.125 → 0.491s**，其中扫描 **62.125 → 0.080s**；命令及进程 **13.598 → 5.367s**；工具回执总时间 **77.262 → 5.861s**。各项之和与总时间之间有调度/回执开销，未重复累加包含项。after spawn 合计约毫秒级，仅代表 Popen 返回，不等于 bwrap 内部启动完成。

扫描根：

- before 每条递归 `/public/home/liusiqi/03.毕设` 和 `/tmp/biomind-latency-before`：共 16 次，其中整个项目 8 次；浅层 304 次。
- after 第 1 条递归 `.../benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-WGS-005`；第 3 条递归该路径的 `medium`；第 2、5、6 条递归 `.../benchmarks/01.测试/04.重测序/Part1.variant_calling/data`。第 4、7、8 条没有递归扫描。共 5 次局部递归、158 次浅层根枚举，整个项目递归 **0 次**。
- 浅层扫描为当前 cwd、被选路径的项目内祖先、任务回执路径和运行环境根；逐条完整绝对根目录、命令、字节数、输出摘要、计时和回执位置见 [measurements.json](measurements.json)。保留根级敏感名称检查，未将“扫描为零”作为提速目标。

第 4 条前后均返回 1（查询的环境别名缺失）。第 7、8 条前后均有被原 head 管道截断的软件错误；退出 0 不等于软件可用于正式 WGS。stdout 字节数前后相同，目录时间戳等内容可能变化。这些结果如实保留，没有替换科学输入或宣告运行环境就绪。

## 测试与未验证范围

- 随附 `reproduce.sh` 在当前工作区实际运行：**71 passed，8 subtests passed**（13.24s）。
- 新增离线验收共 15 项通过。覆盖真实服务准备/执行/取消/超时、动态文件和 deny 变化、含空格路径、cwd 改变、未来输出、wrapper/PATH/JAVA_HOME、批量并发上限和去重、真实跨阶段调用入口、Recovery 回执接纳、CLI 事件/草稿、显示故障、独立 profiler 计数。
- 隔离提交代码的定点套件 **70 passed，8 subtests passed**。导出只包含提交树中的代码，并补充未修改的 tasks.yaml 和 matrix.mtx 测试数据；未加载工作区无关未提交代码。
- 当前工作区扩大回归 **196 passed，3 failed，15 subtests passed**。3 项在本轮修改前的源文件快照回放中全部复现：任务版本反馈 2 项（缺失 capability scope、旧计划修订提示预期）和旧 CLI Recovery fixture 1 项（未声明 finish_reason）。未降低这些检查以让测试变绿。
- 隔离版本的另一组扩大检查为 92 passed、38 failed；同一导出环境下原 HEAD 也是相同 92/38，失败集合完全一致，其中还包括既有 helper 调用契约 fixture 和 Recovery manifest 断言。记录在 [test_results.json](test_results.json)，完整日志在审计导出。没有宣称全仓回归通过。
- 本次修复还消除了既有 formal resolver 的跨项目候选混入：修改前同一套件 7 failed/10 passed；修改后 17 passed。缺失路径与同名文件不自动等价的旧断言按本次要求调整。
- 工作区已有开关保持原值：`BIOMIND_CANDIDATE_VALIDATION_ENABLED` 默认 0，`ENABLE_PLAN_ASSUMPTION_VALIDATION` 默认 true；没有改配置文件或旧 Controller/renderer/资源批准流程。无关 dirty/untracked 内容保留。

未验证：真实模型最终响应、完整计划接纳/批准、恢复原 session、正式 BWA/GATK/WGS、远端主机和冷缓存性能、终端像素级视觉截图。本次使用离线 provider/事件回放和真实只读沙箱；没有给最后被取消请求追加输出额度或断言它最终一定会成功。

## 提交与复现

1. `c33a9478258d97e24b8d3f01e7bd059253df6481`：从已有未提交共享检查实现中隔离必要前置依赖（共享工具、进程清理、阶段接入及原测试）。
2. `573f72c406c306e401a7f2c949be438036f20925`：本轮性能、绝对路径、真实状态显示、度量与定点测试修复。

具体文件范围见 [commit_scope.json](commit_scope.json)。使用独立 index 构建提交树，分支以旧 HEAD 作比较后更新；没有 checkout/reset 工作文件。初始 staged 为空，提交后 staged 为空，无关修改继续留在工作区。

`reproduce.sh` 运行离线定点回归；需现有含 pytest/LangChain 的 Python，通过 `BIOMIND_TEST_PYTHON` 指定，不安装依赖。`measure_checks.py --mode after --project-root <原项目绝对路径> --output <新结果目录>` 可有意重放本组已审计只读检查；before 通过上述前置提交读取旧实现。测量不是启动/恢复原 session。原始前后回执和初始补丁保留在 `/public/home/liusiqi/03.毕设/biomind_audit_exports/planner_latency_fix_20260918`。
