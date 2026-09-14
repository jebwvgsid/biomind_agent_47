# final08 公共调用方回归

独立运行 39 项：38 PASS、1 原有断言 FAIL；另有 2 subtest PASS。实际逐组返回码、载入字节与原始日志见 [verification.json](verification.json)。每组运行前后全部 457 个 manifest 文件 hash 不变。

唯一失败仍是 provider audit.to_dict 已含 generate_call_id/scope/source_ref，而旧断言只接受四个布尔字段。原断言不改，失败不改标 PASS；实施基线相同事实见 [既有证据](../unified_final/preexisting_assertion.json)。安全未运行清单与各组边界保留于本目录账本。

此为候选 08 实际重跑结果，不复用此前结果代替。
