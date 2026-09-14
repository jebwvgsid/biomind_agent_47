# F3 env 精确修正进展补证

当前统一候选 **08 的 F3 33 项通过，其中本文件五项全部通过**；局部 before/after 的当前指针保留为 `after04`。本补证不把旧 final04 的 F6 18 PASS 计为本缺口 FIX。运行入口、实际返回码、生产/测试字节及原始失败保存在 [verification.json](f3_env_progress/verification.json)。

## 首偏差与 before

原 parser 在第一个非法 env 字段处立即返回，只给此前合法字段登记验证事实。于是 `{"BAD":7,"VALID":"ok"}` 变为 `{"VALID":"ok","BAD":7}` 时，完整候选指纹相同，但后者新增 `execute.env.VALID`，造成 validation signature 变化且错误获得进展。新增无关合法键也有相同问题。

`before01` 使用与 final04 完全一致的 `bio_agents.py` / `candidate_repair.py` 字节，在新隔离根上运行两项新增反例：**2 FAIL、rc=1**。其负例函数源码与最终新增测试完全相同；未改冻结旧 export 或旧断言。完整失败与诊断 JSON 均保留，不能用已通过的完整机制链遮蔽该边界。

## 最小实现

仅更改 `BioAnalystAgent._structured_action_from_object_result` 的 env 检查段和 `candidate_progress_assessment`：

- parser 扫描全部 env 字段，将实际合法字段按确定顺序登记；仍原样保留输入顺序中的第一个错误。`env_object`、`env_values` 保留，新增 `env_keys` 仅在全部键名实际合法时成立。
- 完整 env 字段仍进入候选指纹，完整验证事实仍保留。某个 `execute.env.<key>` 新事实只有解决上一精确 `env.<key>` 未决目标时才计入续额进展。仅重排/新增无关字段不能进展；单纯换首错误也不虚报解除。
- 同一 canonical 候选的重复上下文仅比较实际 binding facts 与 contract，representation 诊断集合不再参与重复身份。旧进展行没有新上下文摘要时，从该旧行已有的 binding facts 和 contract 派生比较，原行不写回。新诊断字段本身不能给旧候选续额。
- 新 `env_keys` 检查点仅在上一精确未决项为 `env` 键形状错误时单独计入进展。旧行即使缺此诊断，改无关 prose 或新增无关 env 键也不能获得额外机会；更早 JSON/code/outputs 阶段的真正修正仍有其他实际结构检查点。
- 删除可选合法键不作为表示检查点退步。若当前 env 确为 Mapping、parser 已验证 env 对象，且上一精确未决可选键确实不存在，则以 `removed_optional_field:env.<key>` 记录这项解除。科学/授权要求仍由后续既有准入判断。
- 通用结构、helper 物化和真实语法检查点继续参加原共享判断；没有新增全局验证器、预算类型或科学语义推断。

## after 与限界

`after01` 为第一版四项 **4 PASS、rc=0**；`after02` 补充 env 删除/空键正例，仍 **4 PASS、rc=0**，均保留为中间候选。随后 `before02` 新增旧进展行兼容反例得到 **1 FAIL、rc=1**：同候选、同 binding/contract，仅新增 `env_keys` 诊断便获得进展。中间 `after03` 同一新增测试字节运行 **5 PASS、rc=0，11.08 秒**：

进一步 `before03` 使用与 final05 完全相同的两生产文件字节，证明旧行 + 改无关 prose/新增无关 env 仍可借新检查点续额，**1 FAIL、rc=1**；同一测试同时记录同候选已正确停止。补上上述精确 `env_keys` gain 限制后，当前 `after04` 使用完全相同测试字节得到 **5 PASS、rc=0，10.99 秒**。全部中间成功与失败均保留，当前指针仅指 `after04`。

| 用例 | 直接观察 |
| --- | --- |
| 相同 JSON env 重排（含两坏字段交换首错） | 真实 parser 验证事实一致；同指纹重现；不进展；不虚报已解除字段 |
| 增加无关合法 env 键 | 保留该合法字段诊断；原坏值仍未解决；不进展 |
| 修好上一精确坏值 / 删除可选坏键 / 删除无义务合法键 / 空键修正 | 真实 parser 验证正向解除，下一字段仍坏时允许继续 |
| 最后机会完全合法 | 新建正式 Plan 与审批，经真实 TypedGenerate/parser/CAS/共享 Recovery ledger：5 次固定 provider 响应，4 条拒收记录，剩余额度 0，最后合法 env 仍 `action_ready` |
| 已有旧进展行缺新诊断字段 | 同候选/同 binding/contract 被识别为真重复；无关 prose/新增无关 env 均不进展；旧行内容完全不变 |

最后机会图停止于候选准入；不执行科学代码，也不替代 F6 完整真实 transport/审批/Execute/证据/Commit 组合。当前 445 个导出文件运行后全部 hash 匹配。固定 provider、新合成 Plan/状态、既有 offline guard；未读原 session 或数据 fixture。真实 provider 和真实科学 E2E 仍未运行。
