# 评测、诊断限制与可选 Jev 路由

当前默认 `hybrid`：明确请求走规则，其余由 DeepSeek 解析；Jev 已实现并实测，默认关闭。这里集中说明结论和配置；命令、数据集、评分器及 PG 归档详见 [evals 使用说明](../evals/README.md)。

## 阅读顺序

1. [Jev 实测与默认方案](#jev-results)：为什么接入后仍保留 hybrid。
2. [诊断已知限制](#diagnosis-limits)：哪些结果不能当作准确率。
3. [Jev 接入和配置](#jev-integration)：需要切换时再阅读。
4. [历史实验与失败复核](evidence/history.md#phase2-baseline)：按日期追溯，不改写原始结果。

<a id="diagnosis-limits"></a>
## 诊断已知限制

线上使用结构化假设、E 编号引用、时态 `temporal_scope` 和观察立场 `polarity`；最多 5 次规划和 5 次只读工具调用，总检查预算 60 秒。明确的“状态、日志和业务健康”清单可在原预算内补查，失败检查也计入次数；补查不是任意自然语言清单解析器。

2026-10-06 对已分析过的 6 条案例进行一次回归，5/6 通过，24 次模型请求。保留失败为“检查能力缺失”与 `configuration_error` 分类的语义歧义，没有修改标签使其通过。采用 `trajectory-v2-temporal-polarity` 新评分，不能与旧版 4/6、3/6 直接比较。[原始报告](../evals/reports/2026-10-06-diagnosis-fix/known-regression/report.json)和[详细复核](evidence/history.md#diagnosis-followthrough)均保留。

结构化字段、证据编号和契约通过不保证根因正确；已知回归集不能当作独立泛化测试，预设轨迹回放也不是模型成绩。费用缺少可靠报价或用量时保留未知，不合并不同实验得出“总准确率”。截至 2026-10-08，实验库 16 次运行、420 条样本已纳入[最新恢复验证](v1-delivery.md)。

<a id="jev-results"></a>
## Jev 真实接入与路由对照


2026-10-07，已验证服务器 Key 可用，并完成三组真实 API 对照。**保留 `hybrid` 为线上默认，Jev 可选适配暂不启用。** 本次没有改提示、降低置信度门槛或反复重测来改善分数。

### 同条件结果

运行镜像 `vey-agent:jev-133a2d6`；使用已有 `m3-routing-v1` 的 test 分组，18 个案例各重复 2 次。三组的数据哈希、实现哈希一致，36/36 样本均实际运行，没有跳过。

| 方案 | 契约通过 | P50 | P95 | 模型请求 |
| --- | --- | --- | --- | --- |
| 规则优先＋DeepSeek（hybrid） | 36/36 | 0.696 秒 | 1.122 秒 | DeepSeek 28 次 |
| 规则优先＋Jev＋必要时 DeepSeek | 36/36 | 1.561 秒 | 3.739 秒 | Jev 28 次＋DeepSeek 26 次 |
| 全部交 DeepSeek（model） | 36/36 | 0.775 秒 | 1.093 秒 | DeepSeek 36 次 |

这些是单样本路由耗时，包含规则样本、客户端初始化及网络开销，不包含工具执行或企微投递。不能把它们称为聊天端到端延迟。三组按 hybrid → jev → model 顺序运行，可能受缓存、服务负载影响；不是严格受控的性能实验。

成功响应中的模型为 `jev-1.13.0`、`deepseek-flash`。Jev 使用 3 秒总时限、0.85 置信度门槛；样本总时限 25 秒。初始单次连通验证（英文概况请求、无真实服务目录）约 0.961 秒，独立于中文基准，不纳入上表。

### 决策依据

Jev 链路中的 8 次明确规则命中没有请求模型；其余 28 次 Jev 请求中：

- 2 次高置信度概况查询直接映射只读工具，省去 DeepSeek。
- 21 次分类后仍需 DeepSeek 提取服务、动作、日志参数等。
- 3 次低置信度、2 次超时回退至 DeepSeek。没有因回退丢失这些样本，也没有把失败请求算免费。

当前 Jev 只负责类别判定，只有少量请求能省去参数解析，因此增加了一段串行请求。本次准确性没有提升，P50、P95 均明显高于 hybrid，不满足默认开启条件。保留可选适配和故障回退；线上仍使用 `VEY_ROUTER_MODE=hybrid`，未重建或重启生产服务。

本组未观察到评分器标记的违规修改意图，但评测没有执行工具，不能据此证明执行器安全。操作确认、受保护对象及模型故障边界另由发布版本的 147 项自动测试覆盖。Jev 高置信度也不会绕过这些保护。

费用暂不估算：没有录入两家经核实的价格依据；Jev 的两次超时没有用量响应，总 token 记为未知。hybrid 已知总 token 为 30,871，model 为 39,436；跨提供方 token 数不能直接当作费用。原始报告保留每次有效响应的用量。

### 原始证据与归档

- [hybrid 报告](../evals/reports/2026-10-07-jev-live/hybrid/report.json)：`721bfc886cb243e4be6a18f53ecf17e0`。
- [Jev 报告](../evals/reports/2026-10-07-jev-live/jev/report.json)：`6b29c043bacb401f8ad308b67e8bedc4`。
- [model 报告](../evals/reports/2026-10-07-jev-live/model/report.json)：`d55f965773bf4d60b502430878e3d8c6`。
- [成对比较及归档验证](../evals/reports/2026-10-07-jev-live/archive-and-comparison.json)、[实验配置](../evals/reports/2026-10-07-jev-live/experiment.json)。

三组报告及 108 条样本已追加到独立 PostgreSQL `vey_eval`，累计 16 次实验／420 条样本。归档角色仍仅 SELECT／INSERT，没有 UPDATE／DELETE。原有报告和运行 ID 均保留，未改写历史成绩。

通过生产 HTTPS 后台登录接口验证，评测列表已可读取三个新运行；验证使用的会话已退出。核心、执行器、后台均健康，启动时间未改变；所有本次临时评测容器均已退出并清理。这是接口验收，不包含浏览器点击或布局验收。

模型评测容器只挂载合成案例、结果目录及所需模型 Key，无 Docker socket、运维令牌、生产数据库或企微凭据。结束后临时容器已退出；归档另用只挂载归档凭据与报告的容器。Key 不出服务器、不进入 Git 或报告。

原始文件保留于 `/opt/vey/.local/jev-evaluation-20261007-db29728b/`。这批案例此前已用于开发和发布验收，是已知回归集；18 个独立案例和两次重复不足以证明泛化能力或厂商总体性能。后续若调整路由，应冻结新案例并保存新的实验，不能覆盖本次结果。

<a id="jev-integration"></a>
## Jev 可选路由接入


Key 已于 2026-10-07 验证有效，三组真实对照已完成，见 [评测结果与默认路由决策](evaluation.md#jev-results)。Jev 链路未带来整体延迟收益，线上继续使用 hybrid。

2026-10-07。使用现有 HTTPX 调用 [TypeSafe 官方 API](https://docs.typesafe.ai/api) 的 `POST https://api.typesafe.ai/v1/systemone`，Bearer 认证，默认模型别名 `jev-latest`。没有新增 SDK 或数据库迁移。默认仍为 `hybrid`，配置密钥不会自动开启线上 Jev。

### 请求链路

1. 确认、取消、清空上下文、任务查询由控制分支处理；明确查询／启停指令先走现有规则。
2. 开启 Jev 后，规则未命中才调用一个 Choice 问题，类别为 system、services、query、diagnose、operation、clarify。
3. 仅高置信度的当前宿主机概况和无筛选服务列表，直接映射固定只读工具。明确含糊的请求返回澄清。其他类别交 DeepSeek 解析原始请求及参数；两者类别冲突时澄清，不能把只读类别升级为操作。
4. 缺少 Key、3 秒总时限、HTTP 错误、格式／分布校验失败或低置信度均回退到原 DeepSeek 路由。单次不重试，DeepSeek 再失败则沿用原错误处理；整个任务仍受原 60 秒上限约束。
5. 修改请求照常通过多目标防护、目录解析、执行器保护清单和二次确认。Jev 没有执行器令牌、Docker 接口或修改配置能力。

只发送脱敏的当前消息、必要会话上下文与服务目录；不发送本页日志快照。审计记录提供方、实际模型版本、类别、置信度、分支、耗时及用量；不记录请求头、Key 或上游错误正文。未发出请求的缺钥回退是 routing 事件，不计为付费模型请求。

置信度默认门槛 `0.85` 是待评测的工程起点，不是 85% 准确率，也不能与 DeepSeek 分数直接比较。[官方置信度说明](https://docs.typesafe.ai/confidence)给出 Choice 分布换算方式。本实现校验分布完整、总和、最高项及置信度的一致性，并容忍有限舍入差异；不合法时回退。

### 服务器配置

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

### 对照评测

现有评测新增 `--strategy jev`，与线上规则优先和 JevRouter 共用代码。应在相同数据、分组、重复次数下，对照 `hybrid`、`model` 与 `jev`，保留每次报告。既有数据是已知回归集，不代表独立泛化效果。

```bash
python -m vey.evaluation run evals/datasets/m3-routing-v1.json \
  --split test --strategy jev --mode live --repeats 2 \
  --key-file /run/secrets/deepseek_key --jev-key-file /run/secrets/jev_key \
  --max-model-calls 72 --output .local/evals/jev-routing
```

真实评测必须显式指定非空的两份 Key 文件；不会读取生产 `.env`，不会执行 Docker 工具或发送企微消息。生产机器上使用独立发布镜像容器、仅挂载两个 Key、案例和结果目录运行此命令，不挂载 Docker socket、运维令牌、数据库或企微凭据。无 Key 时可用 `--mode rules` 验证规则覆盖，但不能据此给 Jev 打分。

每个未命中的路由案例开始前预留两次调用预算，实际可能只用一次；剩余不足两次时不启动该案例。下一步规划样本仍只调用 DeepSeek。报告保留 `provider_calls`、`routing_paths` 和逐项 `routing_metrics`，延迟包括完整分类／参数解析链路；不会把 Jev 加 DeepSeek 两次调用计为一次。

默认费用未知。若估算费用，`--pricing` 提供 DeepSeek 报价，`--jev-pricing` 提供 TypeSafe 报价，沿用已有 Pricing schema。价格模型须与响应中的实际版本匹配，两者币种须一致；任一用量／价格缺失时总费用仍为 null，不把失败请求算免费。报告与现有 PG 归档、后台评测展示兼容。

### 验收边界

自动测试覆盖直达、缺钥、低置信度、401／429／529、超时、重定向、无效响应、取消、分类冲突、操作确认、保护服务以及双提供方调用预算。真实路由对照已完成；聊天端到端延迟与实际账单费用未评测。模拟测试不构成真实模型成绩，默认不开启线上 Jev，也不宣称它一定更快或更准确。

首次部署与回退材料见[历史快照](evidence/history.md#jev-deployment)；该快照记录的是填写 Key 之前的状态，不代表当前 Key 尚未验证。
