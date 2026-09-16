# 后续受控 canary（NOT_RUN）

本轮没有授权部署候选政策或真实调用；以下仅是后续验证方案。

1. 在新建隔离会话，显式选择 `BIOMIND_RECOVERY_RESOURCE_POLICY=phase_e_candidate_v2`，记录 exact commit、所需 overlay、客户端/模型和实际输出参数。不要恢复 session_1789521763。
2. 先只使用已构造的失败/证据对象，禁止科学 Execute：具体值 Generate 局部纠正、首因可见、三页后合法决定、持续读页触发收束各一例。核对业务/压缩/物理 retry 的唯一 attempt，reservation、known/partial/UNKNOWN usage 及 elapsed。
3. 分别测试短 directive、常见多语/工具参数和较长但合法的 directive。8,192 当前不覆盖协议最大容量；若远端发生 length，确认零工具/零 directive 接纳，再依据真实分布决定候选值，不静默调全局默认。
4. 测试首个业务请求前压缩、CAS 后请求摘要、completed replay/UNKNOWN 重入、取消及迟到 usage。客户端停止和远端停止/计费分别记录，没有服务端证据仍写 UNKNOWN。
5. 单独核对真实 cache usage 原始字段、逐物理请求输入/输出和缓存命中；不得从静态前缀或本轮 synthetic usage 推算命中率/省费。
6. 只有前述 canary 满足预先约定接受标准后，再另行选择是否启用政策和开展科学 E2E。科学 E2E 需要新的明确授权、输入及环境审查，不在本轮完成项中。

状态：真实模型/远端参数执行 **NOT_RUN**；缓存复测 **NOT_RUN**；科学 E2E **NOT_RUN**；真实 external step_worker **NOT_RUN**；远端取消计费 **UNKNOWN**。
