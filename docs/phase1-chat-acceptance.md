# 第一期聊天链路验收记录

日期：2026-09-30；2026-10-01 完成过期状态复核与清理。环境：已部署的 Ubuntu、Docker、PostgreSQL 16、DeepSeek 与企业微信自建应用。

## 验收口径

用户已从企微客户端发送「服务器概况」并确认收到回复。本轮代表性场景由验收脚本生成符合协议的加密消息，发送至真实公网 HTTPS 回调，走运行中的核心 worker、执行器和实际企微发送接口。入站消息为模拟回调，不冒充用户手动输入；投递成功表示企微接口已接收，不单凭这一点认定手机已阅读。

启停只作用于独立项目 `vey-acceptance/web`。其他项目只执行保护拒绝测试。测试占用并清理配置用户的会话，不修改生产会话时间来缩短等待。HTTP 调试入口始终关闭。

## 已通过的场景

| 场景 | 实际证据 |
| --- | --- |
| 真实公网加密回调 | 合法加密请求返回空 200，进入正式持久化任务队列；重复 MsgId 未增加任务及回复 |
| 默认日志与翻页 | 两页各 100 条测试日志，内容不重叠；虚拟 password 值被脱敏；第一页实际分 4 段发送 |
| 日志双上限 | 请求 500 行时在 10000 字符处截断（本次为 150 行），提供游标；501 行参数被 422 拒绝 |
| 工具范围 | shell 请求被 422 拒绝 |
| 保护清单 | 6 个受保护服务的启动／停止／重启，共 18 个请求全部拒绝 |
| 启停与确认重启 | 真实临时容器停止、启动、重启后状态符合预期；重启改变 StartedAt；重复确认不再次改变启动时间 |
| 取消 | 取消后再确认返回 cancelled，容器时间戳不变 |
| 两分钟确认过期 | 不调整时钟，实际等待 125 秒后确认被拒绝或返回 expired，容器时间戳不变 |
| 清空上下文 | 待确认操作不能继续确认；旧日志游标返回 409 |
| 真实模型排查 | 调用 3 个只读工具，返回带 E1 等证据编号的结论；无 operation_prepared／operation 事件 |
| 核心重启恢复 | 核心停止后注入一个已领取的只读测试任务；实际启动后标记 unknown 并发送恢复通知，无工具执行事件，容器启动时间不变 |
| 日志轮转 | 测试容器采用 1 MB／1 文件轮转，写入约 1.8 MB 合成日志；缓存的旧分页仍与轮转前快照完全一致；新查询能读取最后一条保留日志 |
| 多实例与同名隔离 | 临时扩为 2 个实例，各生成 20 条日志；合并 40 行按时间排序并带容器标识；另建同名服务的其他项目容器未被发现为目标 |

回调场景及恢复通知合计 12 条 outbox 记录，全部 sent，共 19 个实际投递分段。模型排查调用 5 次 API（意图 1 次、规划／总结 4 次），本次用量总计 13132 tokens；这些是单次验收观测，不能作为通用速度或成本基准。

任务证据：确认重启 `0d170d96d5924caeb18701d56ee169d7`，诊断 `76d3c524cf7f40a581d1a9eacd7fda4e`，确认过期 `edcef8b7cd3d43398a9ae92a9ae53e47`，恢复通知 `9403fa3aebe64906ad580d1d66af68a5`。任务和摘要按现有 30 天策略保留，不把原始日志长期归档。

## 会话静默过期

测试任务 `820ac778ee9d45ebae985cb4c9f4c3d3` 于 9 月 30 日 19:10:54（北京时间）创建，会话期限为 19:25:54。等待过程中未修改时间戳。10 月 1 日 11:46:56 复核时，同一会话代次及期限保持不变，上下文为空、任务输入文本已清空、结果审计仍保留；核心拒绝旧任务（expired_session），执行器拒绝旧会话凭证（403）。当时没有排队／执行中的任务或待发送回复。

长时间测试进程已结束，但上一轮终端的最终输出未保留，不能据此确认后台清理发生在到期后的 40 秒内。因此本项记录为“真实过期后的状态及访问拒绝通过”，不声称精确清理时延已验收。复现时应将输出保存在服务器文件中。

10 月 1 日已删除全部临时测试容器，Vey 三个服务均健康；原有 Nginx、PostgreSQL、Redis、MinIO 的启动时间与测试前一致。

## 复现方式

先确认已配置 `vey-acceptance/web` 白名单且企微可向本人发送消息。测试会产生实际企微回复及少量模型费用，应在本人不操作聊天时运行：

```bash
cd /opt/vey
docker compose -f tests/fixtures/acceptance.compose.yaml up -d
docker compose exec -T agent-core python - \
  --callback-url https://YOUR_DOMAIN/wecom/callback < scripts/wecom_acceptance.py
```

上述脚本结束时会清空上下文。轮转与多实例测试前，先在企微发送 `日志 测试服务` 并等到回复，建立新的有效会话。保留脚本生成的合成日志，先完成只允许单实例的启停测试。两个模式必须顺序执行；确保 rotation 已结束，再扩容运行 multi：

```bash
docker compose exec -T ops-executor python - rotation < scripts/log_fixture_smoke.py
docker compose -f tests/fixtures/acceptance.compose.yaml up -d --scale web=2
docker compose exec -T ops-executor python - multi < scripts/log_fixture_smoke.py

# 恢复单实例后，最后进行完整 15 分钟静默测试。
docker compose -f tests/fixtures/acceptance.compose.yaml up -d --scale web=1
umask 077
mkdir -p "$HOME/vey-acceptance-reports"
# 运行期间不能向机器人发消息；保存输出，以免终端断开后丢失证据。
docker compose exec -T agent-core python - < scripts/session_idle_smoke.py \
  > "$HOME/vey-acceptance-reports/session-idle.log" 2>&1
cat "$HOME/vey-acceptance-reports/session-idle.log"
```

本次还创建了一个不启动的同名服务干扰容器（不同项目标签），只用来验证发现隔离，测试后删除。核心重启恢复属于受控故障注入：在没有在途用户任务的维护窗口停止核心，注入 running 的只读任务后恢复核心；没有故意打断真实 Docker 修改。

全部测试完成后使用明确的测试 Compose 文件清理，不能对 Vey 生产项目执行 down：

```bash
docker compose -f tests/fixtures/acceptance.compose.yaml down
```

## 验收边界

- 5 次调用／60 秒上限、网络故障、确认响应丢失等恶劣条件由既有真实 PostgreSQL 集成测试和受控替身测试覆盖；本轮真实模型在 3 次工具调用后结束，没有人为迫使生产模型耗满预算。
- 核心恢复场景是预先注入 running 标记后实际重启，不等同于在 Docker 系统调用过程中断电；后者仍按未知结果处理，不承诺跨 Docker／数据库恰好一次执行。
- 自动证书续签尚未经历一次实际到期续签；异机备份、容量告警、日志自动摘要等仍在进度文档的待办中。
- 手机端交互展示需结合用户确认。本轮不把脚本签名回调表述为人工从手机逐条操作。
