# 缺失观测 warning 与无进展停止

实施基线：`2931895db0c190149e2bbe66f5401080f2d2ff06`，保留原有未提交修改。

## 修改边界

1. **warning**：仅缺少无独立 predicate、无必需值字段的普通观测；其他效果、产物、作用域、审批、真假 predicate、工作区回执校验保持硬约束。
2. **模型裁决**：warning 进入现有 Supervisor 的必审 issue。模型必须覆盖该 issue 并说明接受依据；没有模型审查或仍有疑问时不提交。启用模型审查时，basic 检查遇到此 warning 也会审查；普通 basic 成功仍零模型调用。
3. **提交接线**：Commit 重算报告和约束后才接受；单独保存接受的缺失观测及审查决定，下游和最终审计识别此回执。原始 stdout、观测列表和 `passed=false` 事实不改写，不自动伪造布尔观测。
4. **停止**：revalidate 消耗现有事故预算；再次失败保留原 incident 和父引用。比较 attempt、验证事实摘要、门结果和处理结果，不用报告 ID、引用包装或自由文字判断进展。同一事实重核验仍未解决时，在下一次 Recovery 模型请求前停止；有效验证输入变化可继续，但不突破原预算。
5. **退出接线**：使用原有 `blocked → END → managed stop`，持久化状态和事实报告。此类无进展/预算停止不再调用模型生成总结；重入读取已保存停止状态。

没有修改 Generate、Plan、CLI、审批或前端，没有增加 Agent/Controller。仅扩展现有证据引用准入以支持同 attempt 的 revalidate 父证据，保持原始作用域校验。

## 离线证据

- `tests/test_observation_warning_recovery_stop.py`：真实 Supervisor、Recovery、Commit 和持久化入口；模型为确定性夹具，科学执行为预存夹具事实。
- 接受路径：一次审查、零 Recovery、零新 Execute、一次 Commit；重复 Commit 幂等；下游保留缺失事实与接受依据。
- 拒绝路径：不 Commit；一次补读/revalidate 后仍无进展，后续 Recovery 和终态总结均零模型调用；最终状态持久化为 blocked。
- 另覆盖无审查、独立 predicate 缺失/为假、非零返回码、必需值缺失、普通 basic 零模型、事故版本连续、预算与重放。
- `saved_build_projection.json`：只读 session_1789485273 的已保存计划/回执和本地文件元数据；同一缺失从 `step_effect_incomplete` 转为 `step_observation_review_required`。这不是对历史科学成功的追认。
- `legacy_comparison.json`：两个旧测试文件修改前后均 33 失败、21 通过，失败集合相同。其他包含旧故障注入/纠正用例的扩展运行长时间等待后中止，没有计为通过。

测试使用 `tests/phase_b_1789471955/offline_runner.py`，以空环境启动，禁用 socket 连接和 dotenv，并拒绝读取 `.env*`、`启动.sh`。最终定点结果见 `offline_tests.txt`。

真实模型效果、真实缓存率、科学 E2E：**NOT_RUN**。没有恢复原任务、执行历史脚本或 push。
