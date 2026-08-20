<div align="center">

# EvoAgent

### 面向 Pull Request 的证据驱动代码审查与安全修复服务

规则、LLM 与沙箱 Skill 并行审查新增代码；结论必须通过 Diff 定位、证据和修复门禁。

[![CI](https://github.com/God1007/EvoAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/God1007/EvoAgent/actions/workflows/ci.yml)
[![Security](https://github.com/God1007/EvoAgent/actions/workflows/security.yml/badge.svg)](https://github.com/God1007/EvoAgent/actions/workflows/security.yml)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL](https://img.shields.io/badge/Storage-PostgreSQL-4169E1?logo=postgresql&logoColor=white)](#运行架构)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[快速开始](#快速开始) · [运行架构](#运行架构) · [配置模型](#配置模型) · [API](#api) · [文档](#文档)

</div>

## 它解决什么问题

EvoAgent 接收 GitHub Pull Request 或 Unified Diff，只审查变更中的新增行，输出带文件位置、风险等级、证据、修复建议和测试建议的结构化报告。

默认只运行确定性规则，不需要模型 API Key，也不会把代码发给外部服务。配置一个 OpenAI-compatible 模型后，EvoAgent 会先脱敏，再通过严格的 HTTPS/主机白名单发送请求，并限制输入、输出与响应大小。

## 核心能力

| 能力 | 实现 |
| --- | --- |
| 多角色审查 | Planner 分派 Security、Reliability、LLM 与 Skill；Critic、Test、Synthesizer、Verifier 过滤结果 |
| 精确定位 | 发现必须落在 Unified Diff 的新增行上 |
| 可靠任务 | PostgreSQL 状态机、Checkpoint、事务 Outbox、租户容量门禁 |
| 可靠投递 | 进程内队列用于单机开发；Redis Streams 提供幂等、ACK、租约回收、重试和 DLQ |
| 安全模型出口 | 单路由、凭据脱敏、HTTPS、精确主机白名单、仓库策略、结构化输出校验 |
| 动态 Skill | 内容寻址、签名校验、超时/内存/输出限制与容器沙箱 |
| 保守修复 | 只执行白名单 AST 转换，在独立分支生成原子提交和 Draft PR |
| 可执行证明 | 在本地容器中验证“修复前失败、修复后通过”，不执行宿主机回退 |
| GitHub 自动化 | Webhook 验签和幂等、Diff 拉取、评论 upsert、GitHub App installation token |
| 治理与观测 | JWT/RBAC、多租户、仓库策略、审计日志、Prometheus、OpenTelemetry、RPO/RTO 演练 |

## 运行架构

```mermaid
flowchart LR
    INPUT[GitHub Webhook / REST API] --> APP[EvoAgent]
    APP --> OUTBOX[PostgreSQL + Outbox]
    OUTBOX --> QUEUE[Memory / Redis Streams]
    QUEUE --> REVIEW[Review Loop]
    REVIEW --> RULES[Rules]
    REVIEW --> LLM[Single Model Route]
    REVIEW --> SKILLS[Sandbox Skills]
    RULES --> GATE[Evidence Gate]
    LLM --> GATE
    SKILLS --> GATE
    GATE --> RESULT[Report / PR Comment / Fix PR]
```

任务状态：

```text
PENDING → PLANNING → EXECUTING → REVIEWING → SUCCESS
                  ↘ FAILED / CANCELLED
```

代码结构保持直接：`bootstrap.py` 负责一次性装配；`ReviewService` 协调用例；`ReviewHarness` 执行普通循环；PostgreSQL 是唯一持久化后端。没有插件微内核、LangGraph、远程 Proof 控制面、模型路由控制面或 Redis Cluster 协议。

## 快速开始

### Docker Compose（推荐）

要求：Docker 与 Docker Compose。

```bash
git clone https://github.com/God1007/EvoAgent.git
cd EvoAgent
cp .env.example .env

# 在 .env 中设置至少 32 字节随机密钥和管理员密码
docker compose up --build
```

打开 <http://127.0.0.1:8080>。

`.env` 至少需要：

```dotenv
EVOAGENT_AUTH_SECRET=replace-with-at-least-32-random-bytes
EVOAGENT_BOOTSTRAP_ADMIN_PASSWORD=replace-with-a-strong-password
```

### 直接运行 Python

要求：Python 3.11/3.12 与 PostgreSQL 16+。

```bash
python -m pip install --require-hashes -r requirements.lock
export EVOAGENT_DATABASE_URL='postgresql://evoagent:password@127.0.0.1:5432/evoagent'
python -m evoagent.migrate
python -m evoagent
```

服务默认只监听 `127.0.0.1:8080`。若绑定非回环地址，必须同时启用认证；异步生产流量应配置 Redis。

### 第一次审查

```bash
curl -X POST 'http://127.0.0.1:8080/v1/reviews' \
  -H 'Content-Type: application/json' \
  -d '{
    "repository": "demo/api",
    "pull_request": 12,
    "diff": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n+password = \"secret\"\n+eval(user_input)"
  }'
```

异步提交增加 `?async=true`，再查询 `/v1/tasks/<task-id>`。

## 内置规则

| Rule ID | 等级 | 检查 | 自动修复 |
| --- | --- | --- | :---: |
| `SEC-EVAL` | Critical | `eval()` / `exec()` | — |
| `SEC-SUBPROCESS-SHELL` | High | `shell=True` | ✓ |
| `SEC-HARDCODED-SECRET` | High | 硬编码凭据 | ✓ |
| `SEC-SQL-CONCAT` | High | SQL 字符串拼接 | — |
| `SEC-YAML-LOAD` | High | 不安全 YAML 反序列化 | ✓ |
| `SEC-INSECURE-COOKIE` | High | 不安全 Cookie | ✓ |
| `REL-EMPTY-EXCEPT` | Medium | 吞掉异常 | — |
| `REL-DEBUG-PRINT` | Low | 调试输出 | ✓ |

## 配置模型

只支持一条活动路由。最简单的方式是使用 Provider 预设：

```dotenv
EVOAGENT_LLM_PROVIDER=deepseek
EVOAGENT_DEEPSEEK_API_KEY=...
EVOAGENT_LLM_ALLOWED_HOSTS=api.deepseek.com
```

自定义 OpenAI-compatible 端点：

```dotenv
EVOAGENT_LLM_PROVIDER=custom
EVOAGENT_LLM_BASE_URL=https://models.example.com/v1
EVOAGENT_LLM_API_KEY=...
EVOAGENT_LLM_MODEL=review-model
EVOAGENT_LLM_ALLOWED_HOSTS=models.example.com
```

也可以使用环境变量承载密钥的单路由 TOML：

```toml
version = 1

[[routes]]
id = "primary"
provider = "internal"
model = "review-model"
base_url = "https://models.example.com/v1"
api_key_env = "MODEL_API_KEY"
region = "eu-west"
```

仓库策略仍可限制允许的 provider、model 和 region；不匹配时请求在出站前被拒绝。

## GitHub 接入

生产推荐 GitHub App：

```dotenv
EVOAGENT_GITHUB_APP_ID=123456
EVOAGENT_GITHUB_APP_SLUG=evoagent
EVOAGENT_GITHUB_PRIVATE_KEY_PATH=/run/secrets/github-app.pem
EVOAGENT_GITHUB_WEBHOOK_SECRET=...
EVOAGENT_AUTO_POST_REVIEW=true
```

Webhook 地址为 `/webhooks/github`。服务验证 `X-Hub-Signature-256`、时间窗口、delivery id 和载荷摘要。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/reviews` | 同步/异步创建审查 |
| `GET` | `/v1/tasks/<id>` | 查询任务 |
| `GET` | `/v1/tasks/<id>/report` | 获取 Markdown 报告 |
| `POST` | `/v1/tasks/<id>/feedback` | 记录反馈 |
| `POST` | `/v1/tasks/<id>/fix` | 创建保守修复 PR |
| `POST` | `/v1/proofs` | 在容器中执行修复证明 |
| `GET/POST` | `/v1/repository-policies` | 查询/更新仓库策略 |
| `POST` | `/v1/auth/login` | 登录获取 JWT |
| `GET` | `/health` | 进程存活 |
| `GET` | `/ready` | PostgreSQL/队列就绪状态 |
| `GET` | `/metrics` | Prometheus 指标 |

## 关键配置

| 变量 | 说明 |
| --- | --- |
| `EVOAGENT_DATABASE_URL` | 必填 PostgreSQL URL |
| `EVOAGENT_REDIS_URL` | 可选 Redis Streams；生产异步模式推荐 |
| `EVOAGENT_AUTH_REQUIRED` | 非回环监听必须为 `true` |
| `EVOAGENT_AUTH_SECRET` | JWT 密钥，认证启用时至少 32 字节 |
| `EVOAGENT_TENANT_MAX_ACTIVE_REVIEWS` | 每租户跨副本活动审查上限，`0` 表示不拒绝 |
| `EVOAGENT_REPAIR_CONTAINER_IMAGE` | Proof/修复验证容器镜像 |
| `EVOAGENT_SKILL_CONTAINER_IMAGE` | 不可信 Skill 容器镜像 |
| `EVOAGENT_HISTORY_RETENTION_DAYS` | 历史清理天数，默认 `0`（关闭） |

完整模板见 [.env.example](.env.example)。

## 开发与验证

```bash
export EVOAGENT_TEST_POSTGRES_URL='postgresql://evoagent:password@127.0.0.1:5432/evoagent_test'
python -m ruff check .
python -m ruff format --check .
python -m mypy
python -m pytest -q
```

CI 会创建临时 PostgreSQL 和 Redis，并额外验证迁移、连接池、Streams 租约回收、容器 Proof、备份恢复和端到端 Outbox 投递。

## 质量口径

仓库包含 100 条 `synthetic-controlled` PR Diff 基准。它用于可复现回归门禁，不代表真实线上 PR 效果；报告和数据哈希见 [评测文档](docs/evaluation.md)。自动修复只覆盖可确定转换，无法证明安全时会降级为人工审查。

## 文档

- [架构](docs/architecture.md)
- [模型网关](docs/model-gateway.md)
- [仓库策略](docs/repository-policies.md)
- [事务 Outbox](docs/transactional-outbox.md)
- [数据库迁移](docs/database-migrations.md)
- [灾备](docs/disaster-recovery.md)
- [运行手册](docs/operations.md)
- [威胁模型](docs/threat-model.md)
- [评测方法](docs/evaluation.md)

## License

[MIT](LICENSE)
