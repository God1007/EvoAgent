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
    "diff": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -0,0 +1,2 @@\n+payload = yaml.load(raw)\n+eval(user_input)\n",
    "context": {
      "title": "Harden configuration loading",
      "spec": "Configuration parsing must not execute user-controlled input.",
      "standards": "Use safe loaders for untrusted YAML."
    }
  }'
```

此接口只审查提交的 Diff，不会根据仓库名自动拉取 PR。需要关联编号时添加 `pull_request`；需要固定 Studio 发布版本时添加 `"workflow":{"id":"...","version":1}`。同步调用时，去掉 `?async=true` 和仅用于异步请求的 `Idempotency-Key` 请求头，等待审查结果。

`context` 可选且只接受 `title`、`spec`、`standards` 三个字符串。标题最多 512 UTF-8 字节，需求和规范各最多 32 KiB；未知字段、NUL 或超限内容返回 400。服务端将来源标为 `api`，把完整 Context Pack 固定到任务和 Workflow 输入摘要，但始终按不可信资料处理：它不能授予工具、权限或改变流程连线。GitHub Webhook 自动把 PR 标题和正文写入同一契约；超长正文按 32 KiB 截断并显式设置 `truncated=true`，不会伪装成完整 Spec。若该 GitHub 任务实际请求 Repository Evidence 且规范为空，同一份固定 SHA 归档还会按固定文件名和变更目录作用域生成至多 32 KiB 的 Repository Standards Pack；显式规范不会被覆盖。

请求不接受 `evidence`、自动规范来源或仓库源码字段。若 GitHub Webhook 任务固定的 Studio 流程显式连接 `repository-evidence@1`，Worker 才会从已验证 installation 下载固定 `head_sha` 的归档，并在一次遍历中生成不含源码的 Python 影响摘要与有界 Repository Standards Pack。默认归档/索引上限由 `EVOAGENT_REPOSITORY_EVIDENCE_MAX_BYTES` 控制；失败时任务继续执行，但交接记录明确显示仓库证据不可用。手动 Diff 不会根据 `repository` 名称自动拉取仓库。

异步受理后查询 `GET /v1/tasks/<id>` 查看状态，或请求 `GET /v1/tasks/<id>/report` 获取完成后的 Markdown 报告。未启用认证的回环开发环境可以省略 `Authorization`；对外部署必须启用认证。

`Idempotency-Key` 最多 64 字符。同一租户用相同 key 重试相同请求，会返回原任务及其当前持久化状态；若改变仓库、PR、Diff、Context Pack 或流程版本，则返回 `400`。首次受理返回 `202`，幂等重放返回 `200` 且 `replayed=true`。新的独立审查应使用新的 key。

## 接口索引

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/reviews` | 同步或异步创建审查 |
| `GET` | `/v1/tasks/<id>` | 查询任务 |
| `GET` | `/v1/tasks/<id>/workflow` | 节点状态、当前尝试耗时、连线与版本摘要，不含交接正文 |
| `GET` | `/v1/tasks/<id>/workflow/<step>` | 按需查看实际交接输入与已提交输出 |
| `GET/POST` | `/v1/studio/agents`、`/v1/studio/workflows` | 列出定义或保存草稿；写操作需要 `manage` 权限 |
| `POST` | `/v1/studio/<agents或workflows>/<id>/publish` | 发布指定草稿修订为不可变版本 |
| `POST` | `/v1/studio/validate` | 预检端口、类型、依赖与环路，不执行 Agent |
| `GET/POST` | `/v1/studio/binding` | 查询、绑定或解除仓库的自定义流程 |
| `GET` | `/v1/tasks/<id>/report` | 获取 Markdown 报告 |
| `GET` | `/api/audit` | 当前租户审计记录；需要 `audit` 权限，console 视图不含 detail |
| `GET` | `/api/tenant-review-capacity` | 当前租户审查容量；需要 `manage` 权限 |
| `GET` | `/api/queue/dead-letters`、`/api/outbox?status=dead` | 当前租户的任务/结果投递死信；需要 `manage` 权限，console 视图不含 payload 与错误正文 |
| `POST` | `/v1/outbox/replay` | 按消息 ID 重新派发一条 dead Outbox；需要 `manage` 权限并写入审计 |
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
| `GET` | `/ready` | PostgreSQL、队列及可选 Proof/Repair 能力状态；Proof 含实时 `healthy` |
| `GET` | `/metrics` | 平台管理员可读的全局 Prometheus 指标 |

Studio 的请求结构、版本选择与绑定语义见 [流程 API](agent-workflows.md#api-与当前限制)。仓库策略结构见 [仓库策略 API](repository-policies.md#api)；管理端写入应携带读取到的 `expected_version`，过期版本返回 `409` 且不产生新版本或审计事件。

新建模型 Agent 时使用结构化 `playbook`（`identity`、`objective`、`instructions`）；端口、模型和只读工具仍是独立受校验字段。服务端会追加固定输出契约，Playbook 不能声明额外权限。`GET /v1/studio/catalog` 的 `agent_recipes` 是经过同一校验的草稿初始值，客户端复制后按普通 Agent 保存；它不是可执行引用。旧 `prompt` 形状仅用于不可变历史版本和失败任务恢复，详见流程 API。

## 修复验证

`POST /v1/proofs` 需要 `fix` 权限，接收以下四个字段，拒绝其他字段：

```json
{
  "original": {"app.py": "def clamp(value): return min(value, 10)\n"},
  "patched": {"app.py": "def clamp(value): return max(0, min(value, 10))\n"},
  "reproduction_command": "python -c 'from app import clamp; assert clamp(-1) == 0'",
  "regression_command": "python -c 'from app import clamp; assert clamp(5) == 5'"
}
```

`original` / `patched` 是相对文件路径到 UTF-8 文本的映射，某一版本缺少路径
表示该文件不存在。路径不允许绝对路径、反斜杠或点目录别名；总量仍受 HTTP 请求、
文件数及分析大小限制。复现命令必须在原版失败、补丁版通过；回归命令可省略，
但省略时最高只有 L3。没有复现命令则返回 L1，不执行。

这是同步执行接口，返回 `201` 不代表验证通过：必须检查 `evidence_level`
和 `steps[].status`。超时、执行器不可用和无效回执都不能算作复现证据。
不支持任务 ID、取消或幂等重放；网络中断后不会自动重试，也不要盲目重发。
完整响应含补丁、步骤输出及执行摘要；带 `X-EvoAgent-View: console` 时只保留
页面所需的等级、补丁、步骤状态/耗时及测试输出，隐藏内部摘要与基础设施诊断。

容器由管理员预先配置，调用方不能选择镜像、环境变量、挂载或资源上限。
服务端使用独立 socket 执行器或既有本地容器适配器，未配置时不会在宿主机执行。
它不会直接发布 GitHub 修复；运行边界、超时与部署见
[独立 Proof 执行器](operations.md#dedicated-proof-executor)。

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
- 普通 PR 事件会先校验并固定 `base.sha` 与 `head.sha`，再进入持久化审查队列；Worker 只读取这两个 commit 的 compare diff，即使排队期间 PR 又有 push，也不会把新内容混入旧任务。固定比较点暂时不可读时会重试或失败，不回退到当前 PR；只有升级前创建的旧任务保留 URL 兼容路径。
- 启用自动评论后，服务只为仍是会话最新轮次的任务更新自己的审查评论。
- 草稿 PR 不创建任务；`closed` 或 `converted_to_draft` 会结束会话、取消未完成任务并阻止待发评论。
- `reopened` 或 `ready_for_review` 复用原会话开始新的审查轮次；延迟到达的旧事件不会逆转较新的生命周期状态。

固定 Diff 使用 GitHub 官方的 [Compare two commits](https://docs.github.com/en/rest/commits/commits#compare-two-commits) 接口及 diff media type；GitHub App 的 Contents 只读权限已经覆盖该调用。

### 修复 PR

自动修复只针对支持的确定性规则。发布前需要核对原始 PR head、租户与 installation 绑定，并通过配置的隔离测试环境验证。满足条件后，在独立分支创建提交和 Draft PR，不直接改写原 PR 分支。手动 Diff 没有可验证的 PR 快照，不能用于发布修复。

容器镜像、测试命令和失败处理见 [修复运行说明](operations.md#repair-outcomes) 与 [威胁模型](threat-model.md)。
