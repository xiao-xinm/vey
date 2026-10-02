# 复用 HTTPS 域名访问 MinIO 控制台

启用 `compose.minio.yaml` 后，`https://YOUR_DOMAIN/minio/` 转发至已有 MinIO 的控制台端口。`/minio` 自动补上结尾斜线。沿用 HTTPS 网关证书，企微回调继续使用 `/wecom/callback`。

该地址用于浏览器控制台，不是 S3 客户端 endpoint。9000 端口的 S3 API 不通过此配置公开。已有端口映射和登录凭证不变。

## 配置

1. 在 MinIO 原部署配置中设置 `MINIO_BROWSER_REDIRECT_URL=https://YOUR_DOMAIN/minio/`，重启使配置生效。只配置代理但不设置这个值，控制台会从错误路径加载资源。[MinIO 子路径说明](https://github.com/minio/minio/discussions/15551)。
2. Vey 的 `.env` 设置 `COMPOSE_FILE=compose.yaml:compose.https.yaml:compose.minio.yaml`。默认通过 Linux Docker host-gateway 访问宿主机已发布的 9001；如果端口不同，设置 `VEY_MINIO_CONSOLE_UPSTREAM`。
3. 校验并仅重新创建网关：

```bash
docker compose run --rm --no-deps https-gateway \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose up -d --no-deps --force-recreate --wait https-gateway
```

`compose.https.yaml` 单独使用时仍然只有企微回调路由；新增路由由可选 overlay 显式挂载。MinIO 仍在 Agent 保护清单中。

## 当前服务器的配置持久性

现有 MinIO 是独立容器，数据使用原有命名卷。为保留容器 ID、网络和保护清单，本次使用其已有的 `MINIO_CONFIG_ENV_FILE=config.env`，在容器 `/config.env` 中写入外部控制台地址，并重启原容器。

对应配置副本保存在宿主机 `/opt/vey/.local/minio-console/config.env`。容器普通重启及服务器重启不会丢失这个文件；**删除并重建 MinIO 容器时必须把该配置改为环境变量或只读挂载**，容器可写层不会随数据卷迁移。重建时也要更新 Agent 的受保护容器 ID。

## 回退

从 `COMPOSE_FILE` 移除 `compose.minio.yaml`，仅重新创建 HTTPS 网关即可关闭公开控制台入口，企微回调继续工作。若还需恢复原来的 IP 控制台路径，恢复 MinIO 原配置并重启。不要删除 MinIO 数据卷。

## 2026-10-02 验证记录

- Caddy 配置校验通过，网关健康；正式 HTTPS 下 `/minio` 返回 308 到 `/minio/`。
- 登录页、页面 base 路径、JavaScript／CSS 资源均正常；未登录会话接口返回 403。
- 使用已有账号经 HTTPS 调用登录接口返回 204，认证后的会话接口返回 200。未修改账号、桶或对象。
- MinIO S3 存活检查返回 200，原容器 ID 和数据卷保持不变；仅 MinIO 和 HTTPS 网关为此次配置更新重启。
- 企微签名挑战返回正确明文；公网 `/debug`、`/health`、`/openapi.json` 仍返回 404。
- 本轮验证了 HTTP 与认证接口；浏览器自动化工具无法启动，未将接口测试表述为浏览器交互测试。
