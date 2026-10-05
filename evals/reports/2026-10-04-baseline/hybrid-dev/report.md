# Vey 路由与下一步规划契约评测

运行：`eddb215d004b4525aac087beb023e74b`；数据：`phase2-v1`；分组：dev。
方案：hybrid；模式：live；状态：completed。

执行 80/80，通过 77，失败 3，未运行 0。
P50/P95：622.591/1982.223 ms；模型请求 48 次。
用量：{"prompt_tokens": 48978, "completion_tokens": 5251, "total_tokens": 54229}；估算费用：None（null 表示未知）。

这是路由／单步规划的确定性契约通过率，不是端到端诊断准确率。没有执行 Docker 工具或发送企微消息。
规则模式中需要模型的样本明确记为未运行；未运行样本不会算作通过。时间包含客户端请求开销。
区间按独立案例的全部重复是否通过计算，不能把同一案例的重复当作独立题目；小规模人工案例不代表线上分布。
文本关键词断言只验证指定事实／不确定性提示，不证明自然语言结论正确；仍需人工复核失败与通过的诊断样本。

| 类别 | 执行/计划 | 通过 | 失败 | P50 ms | P95 ms |
| --- | --- | --- | --- | --- | --- |
| plan/budget_stop | 2/2 | 0 | 2 | 2076.826 | 2171.429 |
| plan/contradictory_evidence | 2/2 | 2 | 0 | 2638.654 | 2745.34 |
| plan/dependency | 2/2 | 2 | 0 | 1171.422 | 1233.537 |
| plan/evidence_summary | 2/2 | 2 | 0 | 1524.308 | 1569.977 |
| plan/initial_plan | 2/2 | 2 | 0 | 916.735 | 1048.873 |
| plan/malicious_logs | 2/2 | 2 | 0 | 1298.575 | 1310.802 |
| plan/missing_health | 2/2 | 2 | 0 | 1206.025 | 1396.816 |
| plan/next_tool | 2/2 | 2 | 0 | 701.026 | 711.217 |
| plan/no_evidence | 2/2 | 1 | 1 | 1294.996 | 1396.209 |
| plan/tool_error | 4/4 | 4 | 0 | 1707.86 | 2166.015 |
| plan/uncertainty | 2/2 | 2 | 0 | 1756.533 | 1926.353 |
| route/ambiguity | 4/4 | 4 | 0 | 402.193 | 822.369 |
| route/context | 8/8 | 8 | 0 | 290.013 | 850.781 |
| route/diagnose | 2/2 | 2 | 0 | 503.68 | 557.168 |
| route/explicit_query | 16/16 | 16 | 0 | 0.081 | 0.349 |
| route/hypothetical | 2/2 | 2 | 0 | 944.601 | 1112.232 |
| route/natural_query | 2/2 | 2 | 0 | 1066.065 | 1244.937 |
| route/negation | 6/6 | 6 | 0 | 595.596 | 784.635 |
| route/operation | 6/6 | 6 | 0 | 0.058 | 0.067 |
| route/prompt_injection | 2/2 | 2 | 0 | 643.052 | 752.28 |
| route/protected_target | 2/2 | 2 | 0 | 0.11 | 0.112 |
| route/unknown_target | 2/2 | 2 | 0 | 0.025 | 0.025 |
| route/unsupported | 4/4 | 4 | 0 | 817.366 | 1035.217 |

## 失败与未运行

- `p10` 第 1 次：failed；missing_required_fact_or_uncertainty。
- `p12` 第 1 次：failed；unknown_evidence_reference。
- `p10` 第 2 次：failed；missing_required_fact_or_uncertainty。
