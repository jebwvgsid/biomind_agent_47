# 显示前后（同一事实，不是重新运行原会话）

## 一次执行的多层结果

此前多个打印点依次展开：进程 rc0 → 执行 observation → 局部 VALID → 正式失败 → Recovery 开始；它们没有统一显示活动身份。

现在：

```text
工具进程已启动 · executed.sh；等待执行结果
命令 rc=0，等待正式核验 · executed.sh
正式核验：inspection_required；Commit 尚未确认。
🩺 Recovery started.
artifact.read · artifact:ann_summary：返回第 1–20 行；分页；可从第 21 行继续
步骤完成：finalize_outputs（Commit 已确认）
```

这段是格式说明，步骤完成只在真实 Commit 回执到达时出现。一个 attempt 对应一个可更新的活动；详情保留开始、输出、退出、局部检查、正式核验。plain 无法改写已滚出的行，必要的后到失败追加一次更新。不能隐藏新错误来追求“只有一行”。

## 停止与晚到交付

```text
任务已停止 · blocked
已有 6 步正式提交，完成事实保留。
原始问题：DELIVERY_CONTRACT_UNMET
恢复取证问题：ARTIFACT_LOCATOR_MISSING
停止触发：RECOVERY_NO_PROGRESS
最近已记录调查：10 次模型请求，11 次工具调用；不是全会话累计。
文件交付：not_delivered
系统事实摘要；模型总结未运行。
交付更新：文件交付：not_delivered；目标文件受保护，未覆盖；内部诊断归档=complete
```

上例的错误码/次数由合成真实入口用例提供，不能当作原会话的新终态记录。缺计数来源时显示“未知”，不推成 0。晚回执更新相同 stop_id 的交付字段，原始问题和停止触发器保留。归档成功不表示固定目标已交付。

## 公开消息、工具与输入

| 情形 | 显示行为 |
| --- | --- |
| delta 与 completed 一致 | 完成后仅一个最终正文；event_id 重放不再拼接同一 delta。 |
| completed 修正了 delta | 最终正文采用 completed，预览差异进详情。 |
| 两个请求正文相同 | 两条均保留，按 request/item 身份区分。 |
| 正文未完成时工具事件先到 | 保留正文开始位置，排队保持发生顺序；显示等待不取得业务路由权。 |
| 有 cursor 的截断 | 显示真实分页及可续读位置。 |
| 无 cursor 的工具裁剪 | 明确未提供续读位置，不猜行数或剩余数量。 |
| 查询过宽 / 真正读取错误 / 模型输出截断 | 各用真实回执来源区分，不统一写“报错”。 |
| 审批 | 主屏短 hash，原文与完整 SHA 在原详情入口；查看不算批准。 |
| 运行/恢复/取消/停止/退出 | 输入提示随真实显示事件更新；原输入不改写，控制命令仍由原入口识别。退出擦除活动输入区。 |

默认不展开 stdout、协议 JSON、ownership、inode、长 hash。登录 shell 与执行环境未为此改变；原始 stdout/stderr 仍归执行回执/日志所有，相关执行错误继续在主屏呈现。
