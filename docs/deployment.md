# Ubuntu 部署、接入和恢复

本手册是通用部署操作说明，实际 Docker／PostgreSQL 部署结果见[服务器验收记录](server-acceptance.md)。所有占位名称必须先替换；凭据在服务器本地输入，不要发到聊天或提交 Git。

## 1. 核对现有环境

需要 Docker Engine、支持健康依赖及 start_interval 的较新 Compose 插件，以及已有 PostgreSQL 的管理员访问方式。先记录 Ubuntu／Docker／PostgreSQL 版本、PG 容器名、持久卷、所在 Docker 网络；本地集成测试使用 PostgreSQL 17.11，服务器实际验收使用 PostgreSQL 16.15 和 Compose v5.5.0。

```bash
docker version
docker compose version
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
docker inspect --format '{{json .NetworkSettings.Networks}}' YOUR_PG_CONTAINER
docker inspect --format '{{json .Config.Labels}}' YOUR_PG_CONTAINER
docker inspect --format '{{.Id}}' YOUR_PG_CONTAINER
stat -c '%g' /var/run/docker.sock
```

只读取这些指定字段，不复制完整 inspect（可能含数据库密码）。选用 PG 已有的用户自定义 Docker 网络，填入 `VEY_DATABASE_NETWORK`，应用用容器 DNS 名连接。若 PG 只使用默认 bridge，需要先由管理员配置可共享的用户自定义网络；不要为接入 Agent 把 PG 端口开放到公网。

## 2. 本地配置和身份

将项目放在管理员维护的目录，例如 `/opt/vey`。复制 `.env.example` 为 `.env`、`config/vey.example.toml` 为 `config/vey.toml`，按实际修改：

- `owner_id`：本人企微 UserID；不是昵称。
- `services`：允许管理的 Compose `项目/服务` 与别名；别名歧义将要求澄清。
- `protected_projects`：必须包含 `vey` 和现有 PG 所属项目、其他实际依赖项目；若 PG 没有 Compose 标签，填写完整容器 ID。容器重建后须重新登记 ID。
- 核对上述保护对象后再设置 `database_protection_acknowledged = true`，保持 fail-closed 默认行为。
- `health_url` 仅接受管理员预先配置的地址，且执行器须能访问该地址；若需额外网络，由管理员在 Compose 中显式添加。健康检查不跟随重定向。

Compose 显式设置核心 UID `10001`、执行器 UID `10002`、共享 GID `10000`。先确认这些编号在宿主机不与现有业务身份冲突；冲突时统一改用空闲编号。容器运行不需要给普通 SSH 登录用户加入 docker 组。

```bash
# 在 /opt/vey 下，确认上述 ID 可用后执行。
sudo install -d -o 10002 -g 10000 -m 0770 /opt/vey/run
sudo install -d -o root -g 10000 -m 0750 /opt/vey/secrets
sudo chown root:10000 config/vey.toml
sudo chmod 0640 config/vey.toml
chmod 0600 .env
```

`.env` 的 `VEY_DOCKER_GID` 必须填写上一步查到的 socket GID。不要把 socket 改为 `0666`。`VEY_RUNTIME_DIR=/opt/vey/run`。运行进程不能写保护配置和密钥文件。

创建六个 Compose secret 文件：`executor_token`、`deepseek_key`、`wecom_secret`、`wecom_token`、`wecom_aes_key`、`debug_token`。其中共享执行令牌至少 32 个随机字符；启用本地调试时，使用另一枚至少 32 字符的独立令牌。未接企微时三个企微文件可以为空；缺少任何被 Compose 引用的文件都会导致启动失败。

示例使用 Bash 交互写入一个密钥，输入不会回显或进入命令历史；对其他文件重复此操作：

```bash
read -rs -p 'DeepSeek API key: ' vey_secret_value; printf '\n'
printf '%s' "$vey_secret_value" | sudo tee /opt/vey/secrets/deepseek_key >/dev/null
unset vey_secret_value
sudo chown root:10000 /opt/vey/secrets/deepseek_key
sudo chmod 0640 /opt/vey/secrets/deepseek_key
```

共享执行令牌可用 `openssl rand -hex 32` 生成，直接重定向到相应文件，不打印到聊天。其余文件也设为 `root:10000`、`0640`。`.env` 中生产核心／执行器数据库 URL 含各自账号密码，配置值中的特殊字符须进行 URL 编码；密码中 `$` 等字符也要遵守 Compose 环境文件的引用规则。

## 3. 复用 PostgreSQL 并初始化专用库

先确认 PG 现有持久卷和备份可靠，在该实例**新建专用 `vey` 数据库**，不向业务数据库建表。由管理员在 PG 中执行一次 `CREATE DATABASE vey;`。现有实例上的 `vey_core`、`vey_executor` 角色名须未使用；初始化脚本遇到已有角色会停止，避免接管原有身份。

```bash
docker compose build
# 在当前管理员终端输入连接 vey 专用库的管理员 URL。
read -rs -p 'Migration database URL: ' VEY_MIGRATION_DATABASE_URL; printf '\n'
export VEY_MIGRATION_DATABASE_URL
# 格式：postgresql+psycopg://ADMIN:ENCODED_PASSWORD@PG_DNS:5432/vey
docker run --rm -it --network YOUR_EXISTING_PG_NETWORK \
  --env VEY_MIGRATION_DATABASE_URL \
  --mount type=bind,src="$PWD/scripts",dst=/bootstrap,readonly \
  --entrypoint python vey-agent:0.1.0 /bootstrap/bootstrap_database.py
unset VEY_MIGRATION_DATABASE_URL
```

如果服务器无法访问 PyPI，可指定管理员信任且网络可达的镜像，例如本次验收使用 `docker compose build --build-arg PYTHON_PACKAGE_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`。运行依赖仍使用锁文件中的精确版本和哈希；构建源参数不包含认证凭据。

脚本交互要求两个独立、至少 20 字符的运行账号密码。它执行 Alembic 迁移，创建 `vey_core` 和 `vey_exec` schema，在一个事务内创建角色并授权。两个运行角色均非超级用户、不能建库／建角色，仅可操作自己的 schema 数据；公共 schema 的建表权限在专用库内撤销。迁移管理员凭据不提供给常驻服务。角色在 PG 实例内是全局对象，其他数据库的 PUBLIC 权限仍由现有实例管理员管理。

把两个运行角色的 URL 分别填入 `.env` 中 `VEY_CORE_DATABASE_URL`、`VEY_EXEC_DATABASE_URL`。以后升级只由同一迁移所有者执行 `alembic upgrade head`，不要再次运行首次角色初始化，也不要让应用启动时自动执行迁移。

## 4. 先验证本地入口

```bash
docker compose run --rm agent-core check-config
docker compose up -d --wait --wait-timeout 60
docker compose ps
curl --fail http://127.0.0.1:8080/health/ready
```

核心端口仅绑定宿主机 `127.0.0.1:8080`，执行器没有 TCP 监听端口。部署后先启用 `VEY_DEBUG_ENABLED=true` 进行认证调试（修改环境后重建核心容器），确认能查询测试服务，再验证一次需要确认的测试服务操作。不要用业务 PG 或 Agent 自身做修改测试。

仓库提供独立验收夹具：先在只读策略中登记 `vey-acceptance/web`、配置健康地址 `http://vey-smoke/`，并启用本地调试，然后运行：

```bash
docker compose -f tests/fixtures/acceptance.compose.yaml up -d
docker compose exec -T ops-executor python - --database-only < scripts/acceptance_smoke.py
docker compose exec -T agent-core python - < scripts/acceptance_smoke.py
docker compose -f tests/fixtures/acceptance.compose.yaml down
```

脚本只允许操作上述测试服务，并验证保护对象拒绝、日志、启停和重复确认。根文件系统只读时用标准输入运行，不使用 `docker cp` 放宽限制。验收后将调试开关恢复为 false 并重新应用 Compose 配置。

若在宿主机用 Python 运行本地 CLI：`uv sync --frozen` 后，设置本地路径的 `VEY_EXECUTOR_TOKEN_FILE`／`VEY_DEBUG_TOKEN_FILE` 和 `VEY_DATABASE_URL`，再执行 `uv run vey message '服务列表' --wait`。可通过 SSH 转发访问此本地端口；不要公开代理 `/debug`。调试任务查询返回的是最多 3000 字符的审计摘要，完整日志页通过企微投递。

默认只采集映射的 `/proc` 和 `/opt/vey/run` 所在文件系统的容量。需要其他磁盘时，在 Compose 中添加该文件系统上的固定目录只读挂载，并扩展 `VEY_HOST_DISK_PATHS`；不要映射整个宿主机根目录。上线时与宿主机 `free`、`df`、`uptime` 核对采集口径。

单组件保持一个实例，不使用 Uvicorn 多 worker，也不扩大 Compose 副本数。本版恢复逻辑以此为前提。配置变更需重启两个组件，旧确认会因策略哈希变化而拒绝。

## 5. 接入企业微信

待域名、证书及企微后台要求就绪后，使用现有反向代理仅转发 `/wecom/callback` 至本机核心服务。参考 Caddy 片段：

```caddyfile
ops.example.com {
    @callback path /wecom/callback
    handle @callback {
        reverse_proxy 127.0.0.1:8080
    }
    handle {
        respond 404
    }
}
```

该片段面向代理运行于宿主机的情况。代理若运行在其他容器内，`127.0.0.1` 指向代理自身，需要由管理员配置专用私有网络。不要直接将核心端口改为公网绑定。

配置 CorpID、AgentID、应用 Secret、回调 Token、EncodingAESKey，以及企微后台要求的访问／可信 IP 条件。确认服务器时间同步、出站 HTTPS 可达。回调 GET 完成地址验证；POST 解密后验证企业、应用、本人 UserID 和 MsgId。任务受理及结果经应用消息接口发送给本人；回调不等待模型。

最终真实验收至少包括：签名校验、重复回调、长结果分段、正确本人身份、模型一次自然语言调用、测试容器启动／停止／重启、PG 保护拒绝、超时后状态查询。域名备案及后台资格以用户所属部署地区和企微当前规则为准，本文不把这些条件视为已经满足。

### 5.1 独立 HTTPS 网关（原 Nginx 仅占用 80 时）

仓库提供可选 `compose.https.yaml`。当现有代理无法在不重建的情况下开放 443 时，可以让独立 Caddy 网关使用空闲 443，保留原 HTTP 服务。两种入口方案选一种，不同时占用 443。

准备已解析到服务器的域名，并确认云安全组／防火墙允许 TCP 443。确认 UID 10003 未与宿主机其他身份冲突，然后创建证书持久化目录：

```bash
sudo install -d -o 10003 -g 10000 -m 0700 /opt/vey/tls
```

在现有 `.env` 追加以下设置，不能覆盖原数据库及企微配置：

```dotenv
COMPOSE_FILE=compose.yaml:compose.https.yaml
VEY_PUBLIC_HOST=ops.example.com
VEY_TLS_DATA_DIR=/opt/vey/tls
VEY_ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory
```

`COMPOSE_FILE` 的冒号分隔形式用于 Ubuntu；Windows 需按所在平台改用分号或显式 `-f` 参数。

```bash
docker compose build https-gateway
docker compose run --rm --no-deps https-gateway caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose --profile https up -d --wait --wait-timeout 60
```

网关只发布 443，内部监听 8443，通过私有 ingress 网络代理核心，数据库和执行器不加入 ingress。容器镜像移除了上游 Caddy 二进制的文件 capability，避免与 `cap_drop=ALL` 冲突。内部健康检查通过只证明进程工作，**不证明正式证书已签发或公网可达**。

如需在同一域名开放已有 MinIO 控制台，可显式叠加 `compose.minio.yaml`，见 [MinIO 控制台配置](minio-console.md)。默认 HTTPS 配置不会自动开放这个入口。

使用 TLS-ALPN-01 验证，不占用已有 HTTP 80；其外部入口仍必须是 443。[Caddy 内部 HTTPS 端口说明](https://caddyserver.com/docs/caddyfile/options#https-port)、[ACME 校验配置](https://caddyserver.com/docs/caddyfile/directives/tls#issuers)

先检查网关日志确认测试环境签发成功，再把 `VEY_ACME_CA` 改为 `https://acme-v02.api.letsencrypt.org/directory`，执行：

```bash
docker compose --profile https up -d --no-deps --force-recreate --wait https-gateway
curl --fail https://ops.example.com/wecom/callback
```

上述 curl 不应跳过证书校验；没有签名参数时预期返回 HTTP 422，所以 `--fail` 会报告 HTTP 错误。应检查是否已通过 TLS 验证，再使用带签名的回调验证实际明文响应。根路径、`/debug`、`/health`、`/openapi.json` 预期 404。测试环境证书不能用于企微正式接入。

遇到尚未满足的公网条件时执行 `docker compose stop https-gateway` 停止反复签发；配置和证书目录保留。默认 `docker compose up` 不启用该 profile，恢复入口需要显式 `--profile https`。续期依赖入口长期可达及证书目录可写，应把证书到期检查纳入后续监控。

### 5.2 升级到消息分段进度版本

本次应用需要迁移 `0002`。先确认没有正在执行的任务，保存旧镜像，停止 Vey 核心／执行器并完成专用库备份。使用迁移所有者执行 `alembic upgrade head`，成功后再启动新应用；不要让运行角色获得 DDL 权限。

例如服务器已将迁移 URL 保存在 root-only 的 `/opt/vey/.local/migration.env` 时：

```bash
docker compose stop agent-core ops-executor
# 此处先执行并校验专用库备份，步骤见第 8 节。
sudo docker run --rm --network YOUR_EXISTING_PG_NETWORK \
  --env-file /opt/vey/.local/migration.env \
  --entrypoint alembic vey-agent:0.1.0 upgrade head
docker compose up -d --wait --wait-timeout 60
```

迁移文件中增加 `outbox.sent_parts`，旧行初始化为 0，保留正文、状态和已有尝试次数。旧版本发送中的分段没有进度，升级不能推断此前哪些段已经送达；升级前应先排空投递队列。当前服务器升级前确认任务／投递均为空。不要运行 downgrade 丢弃投递进度；故障回退应使用已验证的备份和对应镜像，并按第 8 节作废旧任务。

## 6. 结果未知与人工核对

执行器超时／崩溃后可能出现 `unknown`：先查询 `操作 <原任务ID>`，再由管理员直接核对 Docker 状态及时间线。当前状态无法证明某次重启是否发生。需要接受未知历史并解除后续修改锁定时，在已完成核对后运行：

```bash
# 连接专用库的迁移管理员环境，在可运行 Python 的维护环境执行。
uv run python scripts/reconcile_operation.py TASK_ID \
  --note '核对时间、容器状态、外部证据和接受未知历史结果的理由'
```

该脚本要求 `VEY_MIGRATION_DATABASE_URL`，只把 unknown 标记为 reconciled，保留原事实和理由；不执行 Docker、不改判成功、不复用旧确认。首次核对应等待可能尚在 Docker daemon 中执行的请求结束。

## 7. 配置模型并验收

将 DeepSeek Key 的完整值写入 `secrets/deepseek_key`，文件权限保持 `root:10000`、`0640`。编辑器可能替换文件 inode，配置后用 `docker compose up -d --no-deps --force-recreate --wait agent-core` 重建核心容器，以重新加载 secret 挂载。

```bash
docker compose exec -T agent-core python - < scripts/model_smoke.py
```

此脚本通过同一身份校验的任务入口提交请求，由已运行的核心 worker 调用真实模型和执行器；不必开启 HTTP 调试接口。它会清空当前配置用户的上下文，并产生实际模型 API 用量。场景仅包括宿主机资源查询和运维助手只读排查，不发送任何修改操作确认。脚本只输出状态、工具名、提示版本和用量元数据，不输出密钥或模型内部思维链。

## 8. 备份与恢复流程

备份应覆盖专用数据库、管理员维护的配置与密钥，以及现有 PG 的原备份策略。采用 `pg_dump -Fc` 生成逻辑备份；使用与服务器版本兼容的 PostgreSQL 客户端。密码使用交互输入或权限为 `0600` 的 pgpass 文件，避免出现在命令行。备份存储到另一故障域并加密，留存周期和目的地在上线前确定。

```bash
# 命令参数使用实际主机、备份角色；不在命令中填写密码。
pg_dump -h PG_HOST -U BACKUP_USER -d vey -Fc -f vey.dump
# 仅恢复到新建隔离库进行演练，绝不覆盖现有业务数据库。
pg_restore -h PG_HOST -U RESTORE_ADMIN -d vey_restore_check \
  --no-owner --no-privileges --exit-on-error vey.dump
```

核对表数量、关键记录、迁移版本、权限及运行角色；`--no-owner --no-privileges` 的隔离恢复不会复制运行授权，上线恢复需由管理员重建授权。演练环境不得启动连接生产 Docker socket 的执行器。

从旧备份正式恢复前，停止核心和执行器并核对当前容器状态。备份可能缺少最新确认消费记录，须由管理员清空恢复库的会话和游标、作废 pending 确认、将 queued/control_queued/running 任务标为 unknown 后再启动，避免旧任务或确认再次执行。此恢复步骤不能靠常规进程重启逻辑代替；应先在恢复演练中验证。

审计保留 30 天，unknown 锁保留到人工核对；日志页缓存、消息投递正文有更短期限。备份保留期独立于在线清理，不能声称数据库删除会同步抹去历史备份。第一版尚无容量告警服务，上线前须纳入现有磁盘容量监控。
