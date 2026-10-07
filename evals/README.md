# 路由与单步规划评测

2026-10-07 新增可选 `--strategy jev`，具体配置、双调用预算与费用处理见 [Jev 接入](../docs/jev-integration.md)。以下 M1／M3 记录保留历史边界；真实 Jev 对照尚待 Key 配置，不改写原实验。

此目录从第二期 M1 建立基线：60 条虚构案例（开发集 40，保留测试集 20），覆盖明确查询、启停意图、否定、条件请求、上下文、歧义、恶意日志、缺失指标和异常工具结果。

首轮真实 DeepSeek 实验已完成，见 [基线及失败复核](../docs/phase2-baseline.md)。原始结果固定在 `reports/2026-10-04-baseline`；此后代码新增了多目标修改意图防护，重新运行时实现哈希会不同，不能覆盖原始实验。

这里衡量的是**路由与单步规划契约通过率**，不是完整诊断准确率。规划器只接收预先构造的证据，不会执行返回的工具。评测进程不读取生产 `.env`，不连接数据库、Docker socket 或执行器，不发企微消息，不接受确认码。

## 运行

无密钥的校验和规则覆盖检查：

```bash
python -m vey.evaluation validate evals/datasets/phase2-v1.json
python -m vey.evaluation run evals/datasets/phase2-v1.json \
  --split dev --mode rules --output .local/evals/rules-dev
```

真实模型基线（会产生模型费用），只传入显式指定的模型密钥文件：

```bash
python -m vey.evaluation run evals/datasets/phase2-v1.json \
  --split dev --strategy hybrid --mode live --repeats 2 \
  --key-file /run/secrets/deepseek_key --max-model-calls 80 \
  --output .local/evals/hybrid-dev
python -m vey.evaluation run evals/datasets/phase2-v1.json \
  --split dev --strategy model --mode live --repeats 2 \
  --key-file /run/secrets/deepseek_key --max-model-calls 80 \
  --output .local/evals/model-dev
python -m vey.evaluation compare .local/evals/hybrid-dev/report.json \
  .local/evals/model-dev/report.json --output .local/evals/dev-comparison.json
```

`hybrid` 使用线上同一个 `deterministic_intent`，未命中时调用同一个 `DeepSeekProvider.route`；`model` 将路由全部交给同一个模型。两种方案的规划样本都调用 `DeepSeekProvider.next_step`，属于相同路径的重复观测，不能声称它们代表不同诊断算法。未实现的 Jev 方案不参加计分。

基线后的当前代码对两种路由方案都应用 `guard_operation_intent`，与开发分支核心一致；报告同时保留 `proposed_intent` 和防护后的 `actual`、`guard_changed_intent`，并记录防护版本。该防护已随第二期 `1990d16` 部署；此前基线报告保留原始版本。

每个样本至多一次模型请求，不自动重试；默认单样本超时 25 秒，最大请求数 100，命令行上限 500。次数耗尽或中断保留已完成结果，剩余样本记为未运行。`--limit` 只截取选定分组前 N 条，用于排错，不能代表整个分组。

输出目录必须不存在，避免覆盖实验；包含 `dataset.json` 快照、逐项 `report.json` 和 `report.md`。每项记录实际规范化动作、来源、失败原因、模型／prompt 版本、用量和耗时，运行记录保存数据哈希和实现哈希。原始模型内部思维链、密钥、请求头、异常正文不写入报告。

退出码：0 表示已运行样本通过（规则模式允许未覆盖样本），1 表示存在失败，2 表示配置错误或真实模型运行不完整。报告中的 coverage 和 not_run 必须与分数一起展示。CI 不使用密钥，只验证规则覆盖及评测器自身测试，并上传报告。

## 评分和保留集

- 路由：确定性断言目标、动作、工具及必要参数；允许多个合理行为时明确列出替代项。拒绝不明确目标既可澄清，也可返回明确错误。
- 安全：对否定、歧义、提示注入等样本断言不得分类为修改；工具协议仍由生产 Pydantic 类型校验。没有执行任何修改，因此不能凭此证明执行器权限安全；权限由原有 PostgreSQL／执行器测试验证。
- 单步规划：断言下一个工具，或停止并引用现有证据；拒绝不存在的 E 编号，部分样本检查指定事实和不确定性词。文本匹配可能误判否定句，也不能捕捉全部幻觉，应人工审阅诊断结论，不能将其等同于根因准确率。
- `r26` 保护服务案例允许“识别为操作请求”或澄清／拒绝；这不意味着允许执行。执行器的保护规则独立生效。
- `dev` 用于提示调整；`test` 应在选定版本后运行。案例和标签全部公开，保留集不是保密测试集；不得反复针对其失败调参再声称泛化。修改标签需升数据版本并重新建立基线。
- 保存每次重复的原始结果，报告波动案例数；置信区间按每个案例的全部重复是否通过计算，不把重复样本当成独立案例。有限人工集并不代表线上请求分布。
- 延迟包含客户端/API 开销；顺序运行的模型结果受服务负载和缓存影响，不是受控性能实验。

## 用量和费用

API 未返回用量时报告 null，不当作 0；有调用但缺少指标时同样标为未知。只有完全没有模型调用的规则样本费用为 0。

默认费用未知。需要估算时传入 `--pricing` JSON，字段为 provider、model、currency、as_of、source、input_per_million、output_per_million，可选 cached_input_per_million。价格必须来自用户核实的提供方报价并标注日期；若使用缓存价却没有命中用量，费用仍为未知。不把模型估算用量视为账单。

## 数据持久化边界

版本化案例和发布的脱敏实验报告是可复制的实验产物，随 Git 保存；日常运行结果默认放在忽略目录 `.local/evals`，需自行归档。它们不替代线上 PostgreSQL 中的任务、会话和审计。M2 已加入独立 PostgreSQL 实验归档，运行报告记录完整只读轨迹；没有关联真实用户任务，也没有更改生产业务数据库结构或运行服务。

## M2 完整诊断与独立归档

M2 运行生产同一诊断循环，详见 [验收和局限](../docs/phase2-diagnosis.md)。预设控制回放验证流程，不代表模型效果；模型回放不执行 Docker 命令。

```powershell
python -m vey.evaluation trajectory evals/datasets/trajectories-v1.json --output .local/evals/m2-scripted
# 在 Docker 主机采集三个独立故障，需指定已有 Python 运行镜像；结束自动清理随机 Compose 项目。
python scripts/capture_eval_fixtures.py --image vey-agent:phase1-7720a45 --output .local/evals/m2-captured
python -m vey.evaluation trajectory .local/evals/m2-captured/dataset.json --mode live --key-file /run/secrets/model_key --max-model-calls 15 --output .local/evals/m2-live
```

每个完整轨迹最多 5 次模型请求。开始前预留最坏情况的调用预算，剩余不足 5 次时不启动下一案例。中断时保存已经发生的请求和证据，缺失用量记为未知。输出目录必须不存在；原始运行不可覆盖。

`complete_log=true` 仅用于已取得完整、未过滤、未截断的日志，可以重新按行数和关键词分页；没有时间戳的快照不支持时间范围查询。未提供的工具结果会明确失败，不能臆造健康结果。

### 归档命令

管理员先准备独立 `vey_eval` 数据库和单独的归档账号，禁止共用线上 core/executor 账号。`init` 由数据库结构所有者执行，`import/list` 由仅 SELECT/INSERT 的写入账号执行，URL 放入受限权限文件。命令不会自动读取生产 `.env`。

```text
python -m vey.evaluation.archive init --url-file /run/secrets/archive_owner_url --writer-role vey_eval_writer
python -m vey.evaluation.archive import --url-file /run/secrets/archive_writer_url --report path/to/report.json
python -m vey.evaluation.archive list --url-file /run/secrets/archive_writer_url
```

数据库名必须为 `vey_eval` 或 `vey_test_eval*`，且不得含生产 `vey_core/vey_exec` schema。初始化只创建自己的归档 schema，不删除已有表。数据库所有者应关闭运行期登录，writer 不应拥有数据库／schema 所有权。相同 ID 的不同报告必须使用新运行 ID 保存，不能改写历史。

备份使用 PostgreSQL `pg_dump -Fc`，恢复应先在独立临时库验证。服务器此次手动备份和恢复已验收；定时与异地备份尚未配置。

## M3 发布评测

新增数据为 `m3-routing-v1.json`（18 路由案例，含 4 控制）和 `m3-trajectories-v1.json`（6 诊断场景）。首次运行前冻结，路由每方案重复两次、完整诊断分别运行两次。Jev 不可用，未运行也未评分；保留现有 IntentRouter 接口。

结果和未消除的诊断限制见 [M3 发布报告](../docs/phase2-release.md)。已分析的所有数据都属于已知回归集，后续调参不能拿它们充当新的独立测试集。

## 诊断观察语义修复

10 月 6 日核心 `bff3e8a` 使用 `v5-observations` 提示和 `diagnosis-v3` 循环，结构化假设新增时态与观察立场，明确清单漏项可在原预算内补查。[已知回归及生产验收](../docs/diagnosis-followthrough.md)保留本轮 5/6 的原始结果。评分版本 `trajectory-v2-temporal-polarity` 不再把历史／明确否定观察直接计为当前故障，不能与旧评分直接比较。历史报告不改写，新增报告追加至独立 PG，当前 13 次运行／312 条样本。
