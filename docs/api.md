# REST API 与 GitHub 接入

服务启动方式见 [快速开始](../README.md#快速开始)。本文面向 API 调用方与 GitHub App 部署者；浏览器操作见 [Workflow Studio](agent-workflows.md)。完整配置项见 [环境变量模板](../.env.example)。

## 认证与页面视图

启用认证后，先通过 `POST /v1/auth/login` 提交 `username`、`password`，获取返回的 `access_token`。需要指定租户时同时提交 `tenant_id`。后续请求使用 `Authorization: Bearer <access_token>`；不要把真实凭据提交到代码仓库或日志中。

普通账号读取任务详情、流程状态和交接产物时，需要携带 `X-EvoAgent-View: console`，获取经过字段白名单过滤的页面视图。完整诊断数据、Agent/流程草稿和发布管理需要 `manage` 权限。这个请求头不会提升权限，详见 [接口权限与当前限制](agent-workflows.md#api-与当前限制)。

## 提交审查

以下示例假设调用端已通过环境变量注入有效的 `EVOAGENT_ACCESS_TOKEN`：

```bash
curl -X POST 'http://127.0.0.1:8080/v1/reviews?async=true' \
  -H "Authorization: Bearer ${EVOAGENT_ACCESS_TOKEN}" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-review-001' \
  -d '{
    "repository": "local/demo",
    "diff": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -0,0 +1,2 @@\n+payload = yaml.load(raw)\n+eval(user_input)\n"
  }'
```

此接口只审查提交的 Diff，不会根据仓库名自动拉取 PR。需要关联编号时添加 `pull_request`；需要固定 Studio 发布版本时添加 `"workflow":{"id":"...","version":1}`。同步调用时，去掉 `?async=true` 和仅用于异步请求的 `Idempotency-Key` 请求头，等待审查结果。

异步受理后查询 `GET /v1/tasks/<id>` 查看状态，或请求 `GET /v1/tasks/<id>/report` 获取完成后的 Markdown 报告。未启用认证的回环开发环境可以省略 `Authorization`；对外部署必须启用认证。

`Idempotency-Key` 最多 64 字符。同一租户用相同 key 重试相同请求，会返回原任务及其当前持久化状态；若改变仓库、PR、Diff 或流程版本，则返回 `400`。首次受理返回 `202`，幂等重放返回 `200` 且 `replayed=true`。新的独立审查应使用新的 key。

## 接口索引

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/reviews` | 同步或异步创建审查 |
| `GET` | `/v1/tasks/<id>` | 查询任务 |
| `GET` | `/v1/tasks/<id>/workflow` | 节点状态、连线与版本摘要，不含交接正文 |
| `GET` | `/v1/tasks/<id>/workflow/<step>` | 按需查看实际交接输入与已提交输出 |
| `GET/POST` | `/v1/studio/agents`、`/v1/studio/workflows` | 列出定义或保存草稿；写操作需要 `manage` 权限 |
| `POST` | `/v1/studio/<agents或workflows>/<id>/publish` | 发布指定草稿修订为不可变版本 |
| `POST` | `/v1/studio/validate` | 预检端口、类型、依赖与环路，不执行 Agent |
| `GET/POST` | `/v1/studio/binding` | 查询、绑定或解除仓库的自定义流程 |
| `GET` | `/v1/tasks/<id>/report` | 获取 Markdown 报告 |
| `POST` | `/v1/tasks/<id>/feedback` | 记录反馈 |
| `POST` | `/v1/tasks/<id>/fix` | 创建保守修复 PR；首次返回 `201`，幂等重放返回 `200` |
| `POST` | `/v1/github/installations` | 发起租户绑定的 GitHub App 安装 |
| `POST` | `/v1/proofs` | 在容器中执行修复证明 |
| `GET/POST` | `/v1/repository-policies` | 查询或更新仓库策略 |
| `POST` | `/v1/auth/login` | 登录获取 JWT |
| `POST` | `/v1/auth/password` | 校验当前密码并轮换密码，撤销旧 JWT/OAuth state |
| `POST` | `/v1/users` | 在当前租户创建成员；仅平台管理员可授予平台角色 |
| `POST` | `/v1/users/status` | 平台管理员全局停用或恢复账号，并撤销旧会话 |
| `GET` | `/health` | 进程存活 |
| `GET` | `/ready` | PostgreSQL 与队列就绪状态 |
| `GET` | `/metrics` | 平台管理员可读的全局 Prometheus 指标 |

Studio 的请求结构、版本选择与绑定语义见 [流程 API](agent-workflows.md#api-与当前限制)。仓库策略结构见 [仓库策略 API](repository-policies.md#api)。

## GitHub 接入

对外部署接收 Webhook 时，需配置完整的 GitHub App 安装与 OAuth 绑定。开启自动评论的示例配置如下，请替换占位值：

```dotenv
EVOAGENT_GITHUB_APP_ID=123456
EVOAGENT_GITHUB_APP_SLUG=evoagent
EVOAGENT_GITHUB_PRIVATE_KEY_PATH=/run/secrets/github-app.pem
EVOAGENT_GITHUB_CLIENT_ID=Iv1.example
EVOAGENT_GITHUB_CLIENT_SECRET=replace-with-client-secret
EVOAGENT_GITHUB_OAUTH_CALLBACK_URL=https://review.example.com/github/oauth/callback
EVOAGENT_GITHUB_WEBHOOK_SECRET=replace-with-at-least-32-random-bytes
EVOAGENT_AUTO_POST_REVIEW=true
```

私钥文件必须在应用进程中可读；容器部署需要额外配置只读挂载，仓库自带的 Compose 不会自动挂载该文件。Webhook 密钥在非回环部署中至少需要 32 字节。

在 GitHub App 中配置：

| 配置项 | 示例 |
| --- | --- |
| Setup URL | `https://review.example.com/github/setup` |
| OAuth Callback URL | 与 `EVOAGENT_GITHUB_OAUTH_CALLBACK_URL` 完全一致 |
| Webhook URL | `https://review.example.com/webhooks/github` |
| Request user authorization during installation | 关闭 |

从 EvoAgent 发起安装后，服务会使用带 PKCE 的 OAuth 校验安装归属，只有当前 GitHub 用户可访问该 installation 时才绑定租户；用户令牌验证后立即丢弃。将流程绑定到仓库不会自动完成 GitHub App 安装或授权。

### PR 事件与审查生命周期

- Webhook 校验签名、时间窗口、delivery id、载荷摘要及仓库/PR 绑定；未绑定的 installation 会被拒绝。
- 普通 PR 事件进入持久化审查队列；启用自动评论后，服务更新自己的审查评论。
- 草稿 PR 不创建任务；`closed` 或 `converted_to_draft` 会结束会话、取消未完成任务并阻止待发评论。
- `reopened` 或 `ready_for_review` 复用原会话开始新的审查轮次；延迟到达的旧事件不会逆转较新的生命周期状态。

### 修复 PR

自动修复只针对支持的确定性规则。发布前需要核对原始 PR head、租户与 installation 绑定，并通过配置的隔离测试环境验证。满足条件后，在独立分支创建提交和 Draft PR，不直接改写原 PR 分支。手动 Diff 没有可验证的 PR 快照，不能用于发布修复。

容器镜像、测试命令和失败处理见 [修复运行说明](operations.md#repair-outcomes) 与 [威胁模型](threat-model.md)。
