# 统一 runtime 与生成作用域

R2/A1/A2 复用现有 runtime evidence ledger、能力 resolver、action approval 与执行前复验。Planner 按已选 Skill/capability 投影已检查环境、路径和状态；无事实时不把声明环境写成已检查环境。一个工具成功不覆盖其他 Skill/capability 的 UNKNOWN。

`runtime_probe_contract.py` 将最终 Skill 的固定 `inspection_probes` 编译成白名单 argv：版本、帮助及已知无参数 usage；禁止任意 shell、Python 代码、安装或输入数据操作。输出逐项记录，退出码和必要帮助文本必须同时满足。每项检查有时限，driver 总时限 50 秒；进程组在正常/异常退出清理，外层超时先让 driver 完成清理。Consultant 的正式 producer、目标安装验证与 RuntimeInspectionAdapter 共用该编译器/输出合同；adapter 保留只读文件系统、独立 PID/网络 namespace 及 fail-closed 行为。没有放开旧通用 shell 语法。

最终 mapping 路线选择 BWA、samtools、Picard、Java；不同时要求 bwa-mem2/sambamba。所选 calling 路线选择 CombineGVCFs；验证的实际启动组合为系统 `/usr/bin/python3` 调用目标 GATK launcher、目标环境 Java，以及明确 PATH/JAVA_HOME。环境值必须属于候选和批准中的脚本，不在批准后重写 argv。`samtools coverage` 独立能力保留 UNKNOWN，不能由前缀存在或其它子命令成功升级成 ready。

A3 的 `prepare_generation` 在现有 Recovery/Plan/安装批准来源上验证恢复授权。要求正式 current capability 和 approved snapshot hash 一致；旧进程必须已结束、有数值退出码、无存活 PID。旧 attempts 经现有 provenance 终结，释放 ownership，并记录既有 execution_epoch 内的明确 retired 集合。`_not_dispatched` 和非空 required 的 guard 保留；只有该合法 epoch 的已终结旧尝试可被排除。新 attempt 出现后 guard 再次生效。旧空 manifest 不回填，必要要求来自正式 revision。

安装授权精确匹配 current work order 的 request/attempt/target/Skill/capability/hash 和 generation_source_scope；消费后的授权不可用于后续失败。typed Recovery 使用既有 envelope、adopted outcome、generation claim 和 causal reference，不以一条历史 retry 文本授权新执行。

最终补充覆盖同 prefix 准备但 subject 不变的场景：若旧观察已是 unresolved/dependency_blocked，精确 available 工单可一次性推进 epoch 并重新观察；成功覆盖某事实后才 supersedes 旧记录，未覆盖事实与历史记录保留。尚无 action attempt 和已有合法退役历史均可处理，详见 `runtime_install_resume.md`。完整自动/已有环境入口、实际 tiny 执行、终态读取见 `dependency_preparation.md` 及 `evidence/final_dependency_preparation.json`。

## 真实软件与受控 adapter 的边界

`evidence/real_runtime_probes.json` 是已安装软件的真实固定 argv 探测；mapping 7 项 AVAILABLE，coverage 为 UNKNOWN/命令不匹配；GATK 5 项 AVAILABLE。`d3_micro_receipt.json` 另记录完整微小科学输入及实际工具执行。

服务器真实 bubblewrap 检查未通过：简单隔离命令输出 `bwrap: loopback: Failed to create NETLINK_ROUTE socket: Operation not permitted`；mapping adapter 返回非零，calling adapter 超时。在外层沙箱之外授权执行同一只读脚本仍未解决。未关闭网络/文件系统保护、未改主机权限、未把此结果写成软件缺失或 AVAILABLE。`real_controlled_adapter.json` 保留该现场结果；这个结果不能被离线 process-leaf 替身测试覆盖成真实主机通过。

另外使用独立 `/tmp` 假工具 `sleep 30` 验证实际进程清理：单项 8 秒超时返回 UNKNOWN 后子进程终止；外层 0.5 秒超时同样终止后代。`evidence/probe_process_cleanup.json` 保存 PID/耗时/结果，未对真实软件或其他进程进行修改。

## 既有测试失败与新链

旧 `test_runtime_evidence_recovery_production.py` / `test_runtime_evidence_transport.py` 夹具在修改前共同基线即未形成有效 Planner epoch/Plan（PLANNER_PROGRESS_RECEIPT_MISSING）。一次仅更新 Planner outcome 标签的诊断尝试仍失败，已撤销该测试修改，没有改生产代码放过旧载体。基线和累计候选的失败节点单列保存。

新 `test_unified_runtime_graph.py` 使用现行 Consultant/Planner 协议、真实 typed graph、runtime evidence、resolver、HITL、runner、Supervisor/Commit；仅外部 provider、受控检查 process leaf 和人工应答替身。tiny shell 真正执行。全量累计结果由主报告给出，不能用各切片 PASS 相加代替。
