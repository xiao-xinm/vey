# 管理后台

入口为 `https://<你的域名>/admin/`。后台独立于聊天工作进程运行，读取现有审计，不能发送消息、调用模型或执行 Docker 操作。

M2 已增加独立评测记录、任务快照导出及本地逐步复核，使用方式、可选 Compose 叠加文件和限制见[评测与离线复核手册](dashboard-evidence.md)。M3 可选启用[服务配置管理](configuration-management.md)，以下只读权限描述指默认部署；启用配置管理后，后台另外只读挂载执行器 socket 目录，并持有独立配置管理令牌，仍不持有运维执行令牌或 Docker socket。

## 功能与数据范围

- 当前保留任务的状态计数、状态筛选和游标分页，每次最多 50 条。
- 任务结果摘要、创建至最后更新的时长、已记录的工具／模型调用次数。
- 事件时间线，查看工具的有限日志摘要、模型版本／耗时／用量元数据、结构化诊断与证据引用。
- 操作状态及有效期；待确认记录超过时间后明确显示已超过有效期，不展示确认码。

数据来自已有记录，不长期保存新的对话或日志副本。每个任务最多展示前 100 条事件，超出明确标记截断。工具事件只在成功返回后记录，因此“已记录工具次数”不一定涵盖所有失败尝试。事件展示编号不是诊断 E 编号，不虚构两者的映射。完整日志、模型内部思维链和未记录的指标无法通过后台恢复。

列表按创建时间和 ID 倒序做游标分页；游标对应任务已被清理时要求刷新。概览与列表是分别查询的，运行中的数据会变化。状态计数是当前保留范围，既不代表全生命周期累计，也不代表诊断正确率。创建至最后更新包含排队时间。

## 身份与权限

登录口令必须为独立随机的至少 32 字符字符串，通过 `VEY_DASHBOARD_PASSWORD_FILE` 挂载。不复用 SSH、企微、模型或数据库密码。浏览器仅持有随机会话 Cookie，设置 `Secure`、`HttpOnly`、`SameSite=Strict` 及 `__Host-` 前缀，不存储在 localStorage 或 URL 中。

会话闲置 15 分钟失效，最长 8 小时；退出、重新登录替换原会话及服务重启均会使相应会话失效。页面不自动轮询，刷新／查看任务属于活动。单进程最多 8 个会话，新登录超过上限会淘汰最旧会话。服务全局每分钟最多 10 次登录尝试，不信任客户端转发 IP；可能因外部大量尝试暂时限制合法登录，但不影响企微工作进程。

登录、退出校验精确 Origin；页面使用 CSP、禁止嵌入、no-store 及文本 DOM 渲染。原始日志中的 HTML 不会作为网页执行。口令仅保存在受限服务器文件中，轮换后需重建后台服务；当前没有多用户、SSO、找回密码或登录历史持久化。

三个 `vey_dashboard` 视图投影任务、事件和操作记录，隐藏原始会话字段与操作确认码；应用输出另外执行脱敏与游标过滤。视图由管理员拥有，运行账号仅有 SELECT 权限，无法直接读原始表或修改视图；`default_transaction_read_only` 和 5 秒语句超时是额外限制。后台不挂载 Docker socket、执行器 socket、核心密钥、模型密钥或企微密钥。已有审计里的自由文本脱敏不能保证识别所有未知格式的敏感数据，因此入口只面向本人。

## 安装

先完成原有数据库迁移和 HTTPS 部署。管理员连接专用 `vey` 数据库，使用交互式初始化脚本：

```bash
python scripts/bootstrap_dashboard.py
```

脚本读取 `VEY_MIGRATION_DATABASE_URL`，提示输入一个独立随机数据库密码，只新建 `vey_dashboard` schema、三个视图和同名角色；不改原有业务表。如果角色／schema 已存在会拒绝覆盖，需要管理员先检查。初始化事务失败会回滚。

在生产 `.env` 配置：

```text
VEY_DASHBOARD_DATABASE_URL=postgresql+psycopg://vey_dashboard:<独立数据库密码>@<PG容器>:5432/vey
VEY_DASHBOARD_IMAGE=vey-agent:<本次冻结版本>
COMPOSE_FILE=compose.yaml:compose.https.yaml:compose.minio.yaml:compose.dashboard.yaml
```

未使用 MinIO 时从叠加文件列表中去掉该项。生成独立后台登录口令至 `secrets/dashboard_password`，设置 root:10000、0640，目录仅管理员可写。容器以 UID 10004 运行。已有 `.env`、配置、密钥不得加入 Git。

```bash
docker compose --profile https up -d --no-deps --wait dashboard
docker compose --profile https up -d --no-deps --wait https-gateway
```

网关只新增 `/admin/` 路由，没有发布后台宿主机端口。新增挂载需要重建网关，会短暂影响 HTTPS 入口；核心与执行器无须重启。`/health/ready` 仅供 Docker 内部探测，不由网关代理。生产需要 HTTPS；HTTP 仅允许 localhost/127.0.0.1 本地开发 origin，Cookie 仍设置 Secure。

在 SSH 终端读取后台登录口令（不要发到聊天或截图）：

```bash
sudo cat /opt/vey/secrets/dashboard_password
```

## 关闭与回滚

先从 Compose 叠加文件移除 `compose.dashboard.yaml` 并重建网关，验证企微回调和 MinIO 仍正常，再停止／删除单独的 dashboard 容器。不执行整个 Vey 项目的 `down`。只读视图可保留，或由管理员在确认无连接后撤销其角色登录、删除视图／schema 与专用角色；不恢复旧业务数据库覆盖新的任务审计。

后台视图不是运行期自动迁移的一部分。恢复数据库或变更底层 schema 后，需要核对视图及独立角色权限，再启动后台；不能通过授予原始表全部权限解决错误。
