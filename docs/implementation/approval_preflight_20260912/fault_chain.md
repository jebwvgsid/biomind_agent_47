# 审批与预检：故障生产链及最小修改点

起稿时实际分支为 `refactor/consultant-controller-authority`，HEAD 为 `53ff7dba6716d30fd5bbe3a508fa9aef5ba0b4d3`。本轮在其工作区增量实施，保留已交付 Plan/结果验收与 CLI V2；最终提交和 AP 内核 PTY 验收另见本目录 README。

**事故归属。** 沿用附件及前轮静态回执：14:09:56 开始等待审批，14:15:52 收到空输入并按已有 Enter 规则批准，相隔 356 秒；14:15:57 才启动预检，随后取消。这段等待属于审批阶段。原运行没有“提示已完成呈现”时点，不能将全部 356 秒称为用户阅读时间，也没有证据证明 FASTQ 死锁。定位来源为指定 session 的 `run_metrics.json`（前轮引用 line 1321；不是稳定字段接口）。本文件不复制、不重新读取原 session 日志。

**确定的显示故障链。** Plan/Code Review 生成完整审查对象 → OutputRouter/page_text/display_print 将长正文送进历史队列 → 终端后台逐项写正文 → human_io 的旧输入出口先输出提示并阻塞 → 后续正文继续写，把提示推离最后一屏。审批解析器仍等人，当前候选尚未运行。旧正文中四次 `capture_output=True` 检查缺父级逐项转发，则是获批执行后的另一个隐患。

| 生产边界 | 最小修改及不变量 |
|---|---|
| `cli_review.show_review`；OutputRouter 审查入口 | 完整批准对象保留，compact 先显示摘要及详情入口，不重复倾倒长正文。 |
| `human_io.safe_input` → `Terminal.read/_present_input` | 同一 owner 协调有限历史批次、审批帧及输入就绪；原批准键义和对象不变，展示失败不变成批准。 |
| `Terminal._flush_history/use_plain`；`PlainTerminal._write_loop/read` | 后台输出与待审批位置协调；plain 正文完成后才写提示。降级保留原 Future；失效请求停止旧 reader，旧 writer 未完成不得启动第二 writer。 |
| `StreamDisplayCopy.feed` | 只识别新封装专用 stderr 进度为当前活动；原始字节仍先捕获，不进入正式 observation parser。 |
| `execution.preflight.run_checks/with_signal_cleanup` | 新父级封装发出开始、真实 spawn、返回、失败/超时/取消及收尾事件；独立归档每项 stdout/stderr，不转发嵌套 marker。 |
| `executor.run_command/_terminate_process_tree` | 仅短检查显式启用局部期限、专用日志及嵌套组清理；原调用默认行为、外层 deadline 和取消责任保留。 |
| `inspect_fastq_batch.main/assess_prefix`；三份 WGS Skill 建议 | 新候选批准前包含封装、路径及可调期限；精确核对嵌套 observation identity，合并一次外层正式说明；旧批准脚本不修改。 |
| `node_context._without_control_metadata`；Recovery 投影及 `_observation_envelope` | 仅模型副本去除完整、合法的专用进度行；实际下轮 ToolMessage 已验，raw source、正式 marker、科学证据及路由保留。 |

源码定位（行号为起稿工作区；以函数名核对最终提交）：[Terminal.read](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/cli_terminal.py:339)、[PlainTerminal.read](/public/home/liusiqi/03.毕设/Biomind_v1/agent_core/cli_plain.py:275)、[run_checks](/public/home/liusiqi/03.毕设/Biomind_v1/execution/preflight.py:25)、[进程组清理](/public/home/liusiqi/03.毕设/Biomind_v1/execution/executor.py:239)。

**期限核对。** 既有 prefix reader 默认 2 秒协作式读期限，不能硬中断阻塞的 OS read；executor 既有 wall 默认 0（未显式设置时关闭），stall 默认 600 秒。新增短检查默认 60 秒/项，可在新候选显式调整，记录默认/参数来源，从对应调用开始计时。它不用于审批等待、BWA/GATK 等长计算，也不增加自动重试。进程返回不等于检查通过，前缀有效不等于全文件或全 gzip 验证。

**OBS 离线结果与界限。** 最终基础验收 14 项通过，4.95 秒；plain 实际写出加强后 17 项通过，3.92 秒（含 3 项既有回归），导出源码哈希不变；四项合成检查在 compact/verbose 有逐项实际输出，off 保留相同活动记录但隐藏普通输出，真实新 batch 脚本输出一个外层 marker，三个有效前缀和一个负观察区分明确。局部超时、取消以及外层 executor → wrapper → 嵌套进程组均有真实回收证明；300000 字节 stdout 与 350000 字节 stderr 完整保留。详见 `obs_matrix.tsv`、`obs_evidence.json`。O07 的期限隔离为组件验证，生产批准至 spawn 另由 AP 回放覆盖。

所有输入均为新建无害 fixture，未恢复 session、调用真实模型/API/WGS或读取敏感配置。不可中断内核 I/O、脱离管理的后代和永久阻塞 writer 不能承诺全部回收；请求取消与确认消失分别记录。旧 progress/display 组合回归在基线和当前同为 20 通过、1 个 ContextVar 外层 flush_public 断言失败，此处未修复或隐去；也不据本轮验收宣称既有 SIGSEGV 已解决。
