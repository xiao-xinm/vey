# Jev 可选路由接入

2026-10-07。使用现有 HTTPX 调用 [TypeSafe 官方 API](https://docs.typesafe.ai/api) 的 `POST https://api.typesafe.ai/v1/systemone`，Bearer 认证，默认模型别名 `jev-latest`。没有新增 SDK 或数据库迁移。默认仍为 `hybrid`，配置密钥不会自动开启线上 Jev。

## 请求链路

1. 确认、取消、清空上下文、任务查询由控制分支处理；明确查询／启停指令先走现有规则。
2. 开启 Jev 后，规则未命中才调用一个 Choice 问题，类别为 system、services、query、diagnose、operation、clarify。
3. 仅高置信度的当前宿主机概况和无筛选服务列表，直接映射固定只读工具。明确含糊的请求返回澄清。其他类别交 DeepSeek 解析原始请求及参数；两者类别冲突时澄清，不能把只读类别升级为操作。
4. 缺少 Key、3 秒总时限、HTTP 错误、格式／分布校验失败或低置信度均回退到原 DeepSeek 路由。单次不重试，DeepSeek 再失败则沿用原错误处理；整个任务仍受原 60 秒上限约束。
5. 修改请求照常通过多目标防护、目录解析、执行器保护清单和二次确认。Jev 没有执行器令牌、Docker 接口或修改配置能力。

只发送脱敏的当前消息、必要会话上下文与服务目录；不发送本页日志快照。审计记录提供方、实际模型版本、类别、置信度、分支、耗时及用量；不记录请求头、Key 或上游错误正文。未发出请求的缺钥回退是 routing 事件，不计为付费模型请求。

置信度默认门槛 `0.85` 是待评测的工程起点，不是 85% 准确率，也不能与 DeepSeek 分数直接比较。[官方置信度说明](https://docs.typesafe.ai/confidence)给出 Choice 分布换算方式。本实现校验分布完整、总和、最高项及置信度的一致性，并容忍有限舍入差异；不合法时回退。

## 服务器配置

Key 文件固定为 `/opt/vey/secrets/jev_key`，只读挂载到核心的 `/run/secrets/jev_key`，不挂载到执行器、后台或网关。部署时仅在不存在时创建空文件，不覆盖已有内容；权限 `root:10000`、`0640`。以编辑器填写，仅一行 Key，不加变量名或引号：

```bash
sudo nano /opt/vey/secrets/jev_key
```

不要将 Key 放入 Git、聊天、命令行参数或 `.env`。现有 DeepSeek Key 保持独立。

`compose.jev.yaml` 必须排在 `COMPOSE_FILE` 最后，晚于配置管理覆盖文件；其只覆盖 `agent-core` 的镜像和 Jev 设置。`.env` 中非秘密配置：

```dotenv
VEY_JEV_IMAGE=vey-agent:<本次已测试的发布标签>
VEY_ROUTER_MODE=hybrid
VEY_JEV_MODEL=jev-latest
VEY_JEV_TIMEOUT=3
VEY_JEV_MIN_CONFIDENCE=0.85
```

密钥在进程启动时读取。填好后先在独立评测容器做真实调用；确认结果后将 `VEY_ROUTER_MODE` 改为 `jev`，执行以下命令加载配置。环境或 Key 文件变更都使用重新创建，避免沿用旧挂载：

```bash
cd /opt/vey
sudo docker compose up -d --no-deps --force-recreate agent-core
sudo docker compose exec -T agent-core vey check-config
```

`check-config` 只显示模式与密钥是否非空，不验证凭据有效性，也不打印 Key。若要快速关闭 Jev，把模式改回 `hybrid` 再重新创建核心即可；无需回滚数据库或修改执行器。

## 对照评测

现有评测新增 `--strategy jev`，与线上规则优先和 JevRouter 共用代码。应在相同数据、分组、重复次数下，对照 `hybrid`、`model` 与 `jev`，保留每次报告。既有数据是已知回归集，不代表独立泛化效果。

```bash
python -m vey.evaluation run evals/datasets/m3-routing-v1.json \
  --split dev --strategy jev --mode live --repeats 2 \
  --key-file /run/secrets/deepseek_key --jev-key-file /run/secrets/jev_key \
  --max-model-calls 72 --output .local/evals/jev-routing
```

真实评测必须显式指定非空的两份 Key 文件；不会读取生产 `.env`，不会执行 Docker 工具或发送企微消息。生产机器上使用独立发布镜像容器、仅挂载两个 Key、案例和结果目录运行此命令，不挂载 Docker socket、运维令牌、数据库或企微凭据。无 Key 时可用 `--mode rules` 验证规则覆盖，但不能据此给 Jev 打分。

每个未命中的路由案例开始前预留两次调用预算，实际可能只用一次；剩余不足两次时不启动该案例。下一步规划样本仍只调用 DeepSeek。报告保留 `provider_calls`、`routing_paths` 和逐项 `routing_metrics`，延迟包括完整分类／参数解析链路；不会把 Jev 加 DeepSeek 两次调用计为一次。

默认费用未知。若估算费用，`--pricing` 提供 DeepSeek 报价，`--jev-pricing` 提供 TypeSafe 报价，沿用已有 Pricing schema。价格模型须与响应中的实际版本匹配，两者币种须一致；任一用量／价格缺失时总费用仍为 null，不把失败请求算免费。报告与现有 PG 归档、后台评测展示兼容。

## 验收边界

自动测试覆盖直达、缺钥、低置信度、401／429／529、超时、重定向、无效响应、取消、分类冲突、操作确认、保护服务以及双提供方调用预算。真实 Jev 效果、端到端延迟和成本对照须待服务器 Key 配置后补做；测试中的模拟响应不构成真实模型成绩。默认不开启线上 Jev，也不宣称它一定更快或更准确。

## 本轮部署记录

- 核心发布 `133a2d6`，镜像 `vey-agent:jev-133a2d6`，ID `sha256:cb54367a01f67fdf2111f0a767f8f3128fa1b67c895dccbf792e1677db380d60`；2026-10-07 20:30（北京时间）启动并健康。
- 本地 **147 项测试全部通过**，包含真实 PostgreSQL；Ruff 检查与格式检查通过。[发布提交 CI](https://github.com/xiao-xinm/vey/actions/runs/37621156236)成功。
- 生产配置检查：`router_mode=hybrid`、`jev_configured=false`、`debug_enabled=false`。空 Key 文件已创建，确认仅核心只读挂载。实际 API 凭据尚未配置，没有调用 Jev，也没有 Jev 真实成绩。
- 通过运行中的 worker 提交内部只读「服务列表」，任务 `84ae80f499a24c6090f71fde29fee295` 成功；一次 services 工具调用，无模型调用、Docker 修改或企微消息。公开后台 HTTPS 返回 200。
- 执行器、后台、HTTPS 网关、PostgreSQL、Redis、MinIO、nginx 的镜像及启动时间未改变。没有数据库结构变更。
- `/opt/vey/.local/jev-133a2d6/` 留存旧 `.env`、旧镜像信息与源码，以及构建、部署和只读验收记录。需要回退核心时恢复其中 `previous.env`，重新创建 `agent-core`；无需恢复数据库。
