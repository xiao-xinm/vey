# 本机备份、恢复演练与容量观察

2026-10-07，按用户选择先完成本机备份和隔离恢复；异地位置稍后确定。此功能属于第三期 M3 的运维部分；配置编辑与版本回滚已另行交付，见[配置管理](configuration-management.md)。2026-10-08 的最新恢复覆盖 16 次实验／420 条样本，见 [V1 复核](v1-delivery.md)。

## 运行方式

在 Ubuntu Docker 宿主机使用系统 Python 3.10+，由管理员按需运行：

```bash
sudo install -d -o root -g 10000 -m 0750 /opt/vey/operations-reports
sudo python3 /opt/vey/scripts/operations_drill.py \
  --container rag-postgres --project /opt/vey \
  --output-dir /opt/vey-backups --report-dir /opt/vey/operations-reports
```

脚本使用已有 PG 容器内的管理员连接，不在命令行传密码。数据库固定为 `vey` 和 `vey_eval`，不备份其他业务库。宿主机维护权限不下放给 Agent 或后台。进程文件锁阻止同一宿主机并行演练；脚本失败退出非零。运行期间不要修改角色、数据库结构、配置或部署文件。

每次创建独立 UUID 目录，根目录和批次目录为 `0700`，数据库 dump、配置包与清单为 root-only。不会自动删除旧备份。当前不创建定时任务，不设自动保留清理，不传输至外部。

## 验证内容与边界

1. 为每个库分别开启导出的只读一致性快照，行数／内容摘要与 `pg_dump -Fc --snapshot` 共用该快照。线上写入继续运行。两个库分别一致，不承诺跨库同一时刻；序列值不具备同样的 MVCC 语义，单独核对事件序列不落后于恢复数据。[PG 快照同步](https://www.postgresql.org/docs/16/functions-admin.html#FUNCTIONS-SNAPSHOT-SYNCHRONIZATION)、[pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html)
2. 备份源文件、Compose、策略、`.env`、密钥与 TLS 配置，拒绝符号链接，比较备份前后文件哈希并逐个校验压缩包成员。包不包含 Docker 镜像、数据库整卷或 MinIO 对象。含密钥的配置包仅保留在服务器受限目录中，当前没有加密。
3. 用源 PostgreSQL 镜像 ID 创建临时实例，保留源管理员名称，以恢复由其持有的后台视图。临时实例无网络、无端口、无宿主机挂载，数据在 tmpfs；内存 768 MiB、CPU 0.75、进程数 128、capabilities 全部移除。仅对该次创建 ID、名称、标签和网络均匹配的容器做恢复、清理。
4. 七个 Vey 角色恢复为 NOLOGIN，不复制密码；重建继承设置及已审核的只读／语句超时设置。恢复表／视图所有者、数据库授权和对象 ACL，核对行数、内容摘要、视图定义和七个角色的有效权限，并以后台只读角色实际查询。视图 SQL 可能在 PG 导入时展开隐式类型转换，因此在隔离实例的临时视图中重新解析两份定义再比较，不能靠删除字符串中的类型转换“修正”差异。
5. 仅在恢复副本中清空会话、日志游标和任务原文；排队／执行中／待确认任务改为 unknown，pending 确认取消、executing 确认改为 unknown，失效投递正文。保留已有 unknown 锁，不启动核心或执行器。检查可重放记录为零后删除临时容器。

本轮小规模演练上限：每库 128 MiB、配置包源文件合计 100 MB、备份文件系统至少空闲 2 GB。达到上限会失败，需重新评估资源预算。当前不是整机灾备、PITR 或可用性切换；无法从约十秒的临时数据库恢复时间推导真实 RTO，也未承诺 RPO。Vey 角色密码须在正式恢复时重设并同步凭据文件。

## 结果与后台

每次私有目录保存 `vey.dump`、`vey_eval.dump`、`configuration.tar.gz`、`restore-manifest.json` 和 `report.json`。清单记录恢复所需角色元数据、数据／视图摘要及授权，不应公开备份材料。

独立报告目录只写白名单维护元数据：`latest.json` 代表最近尝试，`last-success.json` 仅成功后更新。失败记录不会覆盖上一成功记录。报告文件权限为 root:10000 / 0640，不包含配置内容、日志正文或凭据。

部署时在现有 `COMPOSE_FILE` 后追加 `compose.dashboard-operations.yaml`，仅重建后台。该覆盖文件只读挂载报告目录，绝不挂载备份目录；后台无恢复或备份执行 API。`/admin/api/operations` 要求登录，限制报告为 64 KiB，按字段模型过滤，未知字段不出站；缺失、损坏和不完整的“成功”报告都明确标记。

后台“备份与容量”显示最近尝试和最近成功、阶段及清理结果、数据库与表／索引占用。采样超过 24 小时显示过期提示，运行超过 10 分钟提示核查。刷新仅重读文件，不采集实时容量。文件系统数值属于备份所在挂载点；PG 数据库大小不代表整个实例 WAL、其他库或 Docker 镜像占用。尚未实现趋势、定时告警或自动清理。

## 正式恢复步骤

1. 在新建隔离 PostgreSQL 上恢复；核对 `restore-manifest.json`、压缩包和 dump SHA-256，准备与备份匹配的源代码及镜像。不要覆盖仍在运行的业务库。
2. 管理员按清单重建角色与库所有者，使用 `pg_restore --exit-on-error` 保留对象所有者与 ACL，再恢复数据库级 CONNECT 等授权。恢复只读及超时设置；检查表、视图、默认授权与后台读取。角色密码需重新配置，不沿用演练的免密码隔离实例作为生产入口。
3. 旧会话／任务／确认的处置以脚本 `quarantine` 和 [部署恢复流程](deployment.md#8-备份与恢复流程) 为准。核对实际 Docker 状态后再让核心／执行器连接恢复库；unknown 操作只能人工核对解除，不能自动重放。
4. 配置和秘密文件应按各服务 UID/GID 恢复权限；仅恢复到明确的新目录，重新核对域名、证书、企微配置和依赖地址。先完成只读验收，再恢复修改能力。
5. 回退本次后台发布只需恢复旧 `.env`、旧后台镜像并仅重建 dashboard；本次无 schema 迁移，不应为界面回退而倒灌旧库丢失新审计。

实际演练记录见 [本轮验收](operations-recovery-acceptance.md)。异地副本、加密方式、保留周期与定时频率待用户后续确定。
