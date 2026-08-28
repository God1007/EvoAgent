<div align="center">

# EvoAgent

### 面向 Pull Request 的可组合代码审查 Agent 平台

定义 Agent，连接审查流程，让每一条结论都有来源。

[![CI](https://github.com/God1007/EvoAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/God1007/EvoAgent/actions/workflows/ci.yml)
[![Security](https://github.com/God1007/EvoAgent/actions/workflows/security.yml/badge.svg)](https://github.com/God1007/EvoAgent/actions/workflows/security.yml)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[架构设计](#运行架构) · [Agent 内部设计](#agent-内部设计) · [实机演示](#实机演示) · [快速开始](#快速开始) · [文档](#文档)

</div>

EvoAgent 将代码审查拆成可复用的 Agent：安全检查、业务规则、模型分析、结果汇总。你可以直接使用默认审查流程，也可以在浏览器中定义 Agent、连接输入与输出，为不同仓库组装自己的审查流程。

输入一份 Unified Diff，或接入 GitHub PR 事件，EvoAgent 会执行选定流程，生成包含问题位置、风险等级、依据和修复建议的报告，并保留各节点的执行状态与交接记录。默认规则模式无需模型 API Key，适合先在本地体验，再逐步接入模型和 GitHub。

## 核心能力

| 能力 | 你可以做什么 |
| --- | --- |
| 可视化组装 | 在 Workflow Studio 中创建规则、提示词和汇总 Agent，连接上下游，保存草稿、试运行并发布版本 |
| 定制审查标准 | 组合内置安全规则与自定义文本规则；配置 OpenAI-compatible 模型，补充语义分析 |
| 可读的审查结果 | 按风险查看问题，定位 Diff 新增行，阅读依据、修复建议和验证方法 |
| 可追踪的 Agent 交接 | 查看每个节点收到了什么、产出了什么；任务固定流程版本，支持失败后按检查点恢复 |
| GitHub 工作流 | 通过 GitHub App 接收 PR 事件、更新审查评论；满足验证条件的白名单修复可生成 Draft PR |
| 团队协作与运维 | 按租户隔离数据，以角色控制权限，配置仓库策略，并通过审计、指标和链路追踪排查问题 |

## 运行架构

EvoAgent 采用模块化单体架构：**Agent 负责局部判断，Workflow 负责数据流，Harness 负责执行生命周期，应用层负责权限和外部操作。** 这些职责在同一服务内明确分开，扩展审查逻辑时，不必重新实现任务队列、认证或恢复机制。

### 分层与职责

```mermaid
flowchart TD
    ENTRY[浏览器 / REST / GitHub Webhook] --> APP[应用用例：校验、授权与任务受理]
    APP --> STORE[PostgreSQL：任务、输入快照与 Outbox]
    STORE --> QUEUE[Redis Streams / 本地队列]
    QUEUE --> HARNESS[ReviewHarness：任务状态、预算与恢复]
    HARNESS --> FLOW[Workflow：契约、依赖与交接]
    FLOW --> AGENTS[Agent：规则、模型与证据处理]
    FLOW -. 节点检查点与产物 .-> STORE
    HARNESS --> RESULT[生成审查报告]
    RESULT --> PUBLISH[应用用例：结果展示 / 按策略回写 GitHub]
```

| 层次 | 主要实现 | 职责与设计目的 |
| --- | --- | --- |
| 组合入口 | [bootstrap.py](evoagent/bootstrap.py) | 启动时显式装配组件，将依赖选择与业务逻辑分开 |
| 应用用例 | [application/](evoagent/application/)、[service.py](evoagent/service.py) | 处理审查受理、仓库策略、租户容量、PR 会话和结果发布 |
| 任务运行时 | [harness.py](evoagent/harness.py) | 管理任务级状态转换、预算、重试和阶段检查点，不决定某个检查规则如何实现 |
| 流程执行器 | [workflow.py](evoagent/workflow.py) | 校验 Agent 连线，按依赖执行并提交节点产物，不内置 PR 审查规则 |
| 审查能力 | [agents.py](evoagent/agents.py)、[studio.py](evoagent/studio.py) | 实现默认审查角色，以及使用者定义的规则、模型和汇总 Agent |
| 外部依赖 | [ports.py](evoagent/ports.py) | 用窄接口隔离模型、代码托管、队列和验证执行器；数据库统一采用 PostgreSQL |

这里区分两种检查点：Harness 保存「解析、执行、生成报告」等任务阶段；Workflow 保存阶段内部每个 Agent 的交接结果。节点失败时可以复用已完成的上游产物，而不是从头重新调用所有 Agent。

### 为什么选择这些传输方式

| 边界 | 方式 | 原因 |
| --- | --- | --- |
| 用户或外部系统 → 服务 | HTTP + JSON | 统一浏览器、REST 和 Webhook 入口，在进入执行流程前完成校验与授权 |
| 任务 → Worker | Redis Streams；本地开发可用进程内队列 | 以整项审查任务为投递单位，处理排队、消费确认和失败重试 |
| 同一 Worker 内的 Agent → Agent | 受约束的 JSON 数据副本 + 函数调用 | 保留明确的数据契约和隔离，不为每条流程连线增加一次网络调用 |

PostgreSQL 是持久化状态的唯一事实来源。任务与 Outbox 事件在同一事务提交，避免「任务已保存，但还没入队就宕机」造成任务丢失；Redis 负责投递，不是审查结果的唯一保存位置。详见 [事务 Outbox](docs/transactional-outbox.md)。

目前不把每个 Agent 部署成微服务：流程分支按确定顺序调度，默认 `specialists` 节点内部另有有界并行池。未来若确实需要某类 Agent 独立扩缩容或跨语言运行，可在其执行函数后接远程服务，并继续遵守同一交接契约；当前并不提供分布式 DAG 调度。

## Agent 内部设计

在 EvoAgent 中，一个 Agent 是 **身份与版本 + 输入输出契约 + 执行函数**。执行函数可以是确定性规则，也可以调用模型；不是每个 Agent 都需要一段 Prompt 或一次 LLM 请求。

### 1. 身份：区分组件、流程位置和执行版本

| 标识 | 回答的问题 | 为什么需要 |
| --- | --- | --- |
| `agent_id` | 使用的是哪个 Agent 组件？ | 区分可复用的能力，不依赖界面上的展示名称 |
| `agent_revision` | 使用的是哪一版 Agent 定义？ | 以 SHA-256 摘要绑定组件实现 / 行为配置；Studio 从定义生成，Python 扩展由提供者绑定 |
| `step_id` | 这个组件位于流程中的哪个节点？ | 同一个 Agent 可以被多次复用，每个节点仍有独立的连线、状态和产物 |
| `task_id`、`workflow_revision` | 属于哪次任务、哪一版流程？ | 将执行和结果绑定到原始任务，防止跨任务、跨版本混用 |
| `execution_revision` | 本次执行使用哪套运行配置？ | 将应用源码、能力清单、模型配置和实际 Prompt 等纳入恢复校验，补足单个组件版本之外的依赖 |

身份不等于权限。用户与租户身份由服务端认证确定，模型出口策略从任务绑定的仓库策略解析；给 Agent 起名「管理员」，或在 Prompt、上游输出中写入 `permissions`，都不会获得额外权限。受信任的 Python 扩展则属于部署代码，不能把这种接口约束误认为代码沙箱。

`AgentSpec` 固定组件定义，`Step` 固定一次使用及其连线，`Workflow` 固定整张图。组件、节点和流程分开定义，才能做到「换一个检查实现」「复用同一个 Agent」「调整执行流程」互不混淆。

### 2. 上下文：按需传入，而不是共享全部历史

| 上下文 | 内容与来源 | 谁使用 |
| --- | --- | --- |
| 任务快照 | 原始 Diff、仓库与 PR 信息、仓库策略、选定流程及执行版本 | 应用层与恢复逻辑；不会整包自动传给每个 Agent |
| 节点输入 | `Handoff.inputs` 中明确连线的原始输入或上游产物；`sources` 记录真实来源 | 当前 Agent |
| 执行控制 | 当前任务、节点、尝试次数、幂等键、执行代次、截止时间 | 执行器与 Agent 的执行函数，不作为模型自行授予的权限 |
| 模型消息 | 配置的 Prompt、输出格式要求、已连线输入和预先选定工具的结果 | 通过受控模型网关发送给模型 |

例如，默认 `critic` 只接收解析后的变更与候选问题；`synthesize` 只接收候选问题、质疑结论和静态验证结果，不接收原始 Diff。需要什么由端口和连线声明，而不是让每个 Agent 从全局字典中自行找数据。

上下文管理包含三个具体约束：

- **数据隔离**：交接时通过 JSON 序列化生成独立副本。`inputs` 顶层只读，嵌套数据属于当前接收方；修改自己的列表不会污染上游或其他分支。
- **内存与长度控制**：执行器按最后使用位置释放不再需要的内存产物，数据库检查点仍按保留策略保存。单份交接数据上限为 8 MiB，这不是进程总内存上限；模型网关另行检查输入 token 估算、输出 token 与响应大小。超过预算会拒绝，不静默截断证据。
- **出站保护**：模型只收到当前请求构造的消息；凭据模式脱敏作用于出站副本，不改写保存的原始证据。脱敏不是完整 DLP，仍需遵守仓库数据策略。

这里的「上下文管理」是显式选择、隔离、限额和恢复，不是自动摘要或长期记忆系统；当前没有自动压缩聊天历史、向量记忆或全仓库 RAG。

### 3. 接口契约：先确认能接，再允许执行

Agent 的执行入口是 `run(handoff: Handoff) -> dict[str, Any]`。每个命名输入、输出端口都声明 `PayloadType(name, version, validate)`，例如 `unified-diff@1`、`review-findings@1`。

以 Studio 中的双检查流程为例，两条连接可以写成：

```text
security.findings  → merge.security  : review-findings@1
business.findings  → merge.business  : review-findings@1
```

端口名称可以不同，类型名称和版本必须精确匹配。`merge` 只有收到两个完整输入才会执行，不能把一段普通文本直接当成问题列表。

契约检查分三步：

1. **组装时**：检查节点与来源是否存在、输入是否齐全、类型是否兼容、是否形成环路。
2. **发布产物时**：检查输出端口集合、字段类型、JSON 可序列化性和大小；校验函数不能悄悄修改数据。
3. **接收产物时**：下游用自己的校验器再检查一遍，恢复时还要核对身份和内容摘要，不能仅凭「类型名相同」就信任数据。

`review-findings@1` 约束问题位置、风险级别、证据、建议和置信度等字段；但结构正确不等于结论正确，证据质量仍由审查角色负责。路由权也属于执行器：Agent 输出一个 `next_agent` 或 `recipient` 字段，不能改变流程走向。

### 4. 交接：提交产物后，下游才开始

交接的单位是可验证的产物，不是「上一位 Agent 说自己完成了」。持久化执行遵循以下顺序：

```text
核对流程、实现与原始输入快照
  → 从明确来源重建并校验本节点输入
  → 保存 running 状态与交接身份
  → 调用 Agent
  → 校验输出，原子提交本节点全部产物
  → 读取数据库中已提交的结果
  → 下游重新校验后继续
```

- **失败恢复**：已完成节点读取检查点，失败或中断节点重新执行；输入、实现或流程版本不匹配时拒绝续跑旧任务。
- **并发与取消**：以先提交的完整结果为准，后到的 Worker 不能覆盖它；执行代次 `generation` 防止旧 Worker 在任务恢复或取消后提交过期结果。
- **副作用幂等**：`idempotency_key` 绑定任务、版本、节点和输入，不随重试次数变化。它用于外部系统去重，但不意味着任意外部操作天然只执行一次。
- **日志与状态分离**：`AgentMessage` 用于解释审查过程，检查点才是交接和恢复依据。日志中出现「完成」不能替代产物提交。

这解决的是「哪个版本的谁，把哪些输入处理成了什么，接收方能否安全继续」。完整字段和恢复语义见 [交接协议](docs/agent-workflows.md#3-关键交接不是把聊天记录拼接给下一个-agent)。

### 5. 默认审查：将发现问题与判断证据分开

默认流程并不让单个模型同时承担发现、裁决与执行修复，而是将职责拆成以下节点：

| 角色 | 接收与处理 | 交付 |
| --- | --- | --- |
| Planner | 根据变更文件整理语言、风险域与审查分工 | 审查计划 |
| Specialists | 运行安全、可靠性规则，以及配置的模型 / Skill 检查 | 带源码依据的候选问题 |
| Critic | 检查问题是否落在新增行，证据是否匹配，解释和建议是否具体 | 接受 / 质疑意见 |
| Test | 对照变更行检查静态复现依据 | 静态验证结果 |
| Synthesizer | 结合问题、质疑和验证结果，筛选、去重并排序 | 汇总后的问题 |
| Fix | 对修复建议做保守的文本检查，排除部分危险建议 | 建议可接受性标记 |
| Verifier | 根据置信度、静态验证和建议标记做最终筛选 | 可进入报告的问题 |

这里的 Test 是静态证据检查，Fix 不是写代码，Verifier 也不是容器测试。实际修改仓库由独立修复用例处理，需要核验 PR 快照和权限；可执行验证走容器化的修复 / Proof 路径。自定义流程可以复用这些角色，但不会自动继承所有默认门禁，发布者需要保留所需的检查步骤。

### 6. 可插拔的范围：行为、连线和工具分开配置

- **使用者定制**：在 Studio 中定义规则、Prompt、输入输出和汇总逻辑；保存草稿、试运行、发布不可变版本，再绑定仓库。每个任务固定使用选中的版本，后续改草稿不改变旧任务。
- **开发者扩展**：用 `AgentSpec` 提供新的受信任执行函数，再通过 `Step` 接入流程；模型、队列、代码托管和验证环境在各自接口边界替换，不依赖修改流程执行器。
- **工具能力**：当前 Studio 模型 Agent 使用预先选定的 `local-rules`、`diff-summary` 工具结果，并由网关检查模型和仓库策略；模型不能临时要求执行任意 Shell、导入 Python 或改换下游 Agent。

当前是有限无环流程，支持分支、汇合和组件复用，不支持循环或人审挂起。Python handler 的截止时间与取消检查是合作式约束；不可信代码仍需走受限 Skill / 容器执行路径。

安装 Python 依赖后，可以运行 [自定义流程示例](examples/custom_review_workflow.py)：它保留默认审查链，在末尾接入业务规则 Agent，无需数据库、Docker 或模型凭据。

```bash
python examples/custom_review_workflow.py --check
python examples/custom_review_workflow.py
```

## 实机演示

### 在画布上搭建审查流程

左侧选择或创建 Agent，中间连接审查步骤，右侧配置输入来源。下图将安全检查和业务检查的结果交给同一个汇总 Agent。

![Workflow Studio：组合检查 Agent 并配置交接关系](docs/images/studio-composition.jpg)

### 从报告追溯到执行过程

报告集中展示风险摘要和问题卡片；展开任务后，可以查看节点状态，以及上下游实际交接的内容。

![审查报告：风险摘要、问题位置与修复建议](docs/images/review-report.jpg)

以上截图来自本地规则流程的实际运行。完整的组装、试运行、报告和交接演示见 [操作演示](docs/demo.md)。

## 快速开始

### 1. 启动服务

准备 Docker 与 Docker Compose，然后获取项目：

```bash
git clone https://github.com/God1007/EvoAgent.git
cd EvoAgent
cp .env.example .env
```

编辑 `.env`，将下面三个值替换为自己的配置。认证密钥需为至少 32 字节的随机值：

```dotenv
EVOAGENT_AUTH_SECRET=replace-with-at-least-32-random-bytes
EVOAGENT_BOOTSTRAP_ADMIN_USERNAME=admin
EVOAGENT_BOOTSTRAP_ADMIN_PASSWORD=replace-with-a-strong-password
```

启动应用、PostgreSQL 和 Redis：

```bash
docker compose up --build
```

Compose 会自动完成数据库迁移。打开 [本地控制台](http://127.0.0.1:8080)，使用刚设置的管理员账号登录。首次登录成功后，从 `.env` 移除两个 `EVOAGENT_BOOTSTRAP_ADMIN_*` 变量；已创建的账号会保留。

<details>
<summary>不使用 Docker：直接运行 Python</summary>

准备 Python 3.11 / 3.12 和 PostgreSQL 16+，预先创建数据库及有权访问它的账号：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
export EVOAGENT_DATABASE_URL='postgresql://evoagent:password@127.0.0.1:5432/evoagent'
python -m evoagent.migrate
python -m evoagent
```

将示例连接串替换为自己的数据库地址。此方式通过环境变量配置服务，不自动加载 `.env`。默认监听 `127.0.0.1:8080`，使用进程内队列，不启用登录；本地规则审查不需要 Redis、Docker 或模型凭据。对外部署需启用认证并配置 Redis，详见 [运行手册](docs/operations.md)。

</details>

### 2. 完成第一次审查

进入「发起审查」，仓库填写 `local/demo`，PR 编号留空，保留默认流程，并粘贴以下 Diff：

```diff
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,2 @@
+payload = yaml.load(raw)
+eval(user_input)
```

点击「开始多 Agent 审查」，在任务中心查看结果。这个示例会触发不安全 YAML 加载与动态代码执行检查；它只审查提交的文本，不会执行其中的代码，也不会访问 GitHub。

### 3. 组装自己的流程

进入「Agent 搭建」，创建两个规则 Agent 和一个结果汇总 Agent，将两个检查结果连接到汇总节点。保存草稿后，用同一份 Diff 试运行，检查报告和交接内容，再发布版本。

发布的流程可在「发起审查」中选择，也可绑定仓库，作为后续审查的默认流程。详细步骤见 [十分钟组装三个 Agent](docs/agent-workflows.md#十分钟体验自己组装三个-agent)。

### 4. 按需接入模型与 GitHub

| 接入项 | 用途 | 配置说明 |
| --- | --- | --- |
| 模型服务 | 在规则检查之外增加语义分析，或运行自定义提示词 Agent | [模型网关](docs/model-gateway.md)、[环境变量模板](.env.example) |
| GitHub App | PR 提交或更新后自动审查，并将报告回写评论 | [GitHub 接入](docs/api.md#github-接入) |
| 修复验证容器 | 验证支持的确定性修复，再创建独立修复分支与 Draft PR | [修复运行说明](docs/operations.md#repair-outcomes)、[安全边界](docs/threat-model.md) |

自动修复需要真实 PR 快照、GitHub 权限和隔离验证环境；普通 Diff 审查不需要这些配置。

## 文档

| 想了解什么 | 从这里开始 |
| --- | --- |
| 看完整操作过程 | [实机演示](docs/demo.md) |
| 定义 Agent、连接流程、理解交接 | [Workflow Studio 与交接协议](docs/agent-workflows.md)、[自定义流程示例](examples/custom_review_workflow.py) |
| 集成自己的系统或 GitHub | [REST API 与 GitHub 接入](docs/api.md)、[模型网关](docs/model-gateway.md) |
| 理解架构和技术取舍 | [架构分层](#运行架构)、[Agent 内部设计](#agent-内部设计)、[模块说明](docs/architecture.md)、[设计决策记录](docs/adr/) |
| 部署、升级和排障 | [运行手册](docs/operations.md)、[数据库迁移](docs/database-migrations.md)、[灾备恢复](docs/disaster-recovery.md) |
| 管理权限与安全策略 | [仓库策略](docs/repository-policies.md)、[威胁模型](docs/threat-model.md) |
| 查看测试与效果数据 | [集成验证](docs/integration-testing.md)、[评测方法](docs/evaluation.md)、[评测结果](docs/evaluation-baseline.md)、[性能测试](docs/performance.md) |

项目当前处于 Alpha 阶段。生产部署前请阅读 [待完成的验收项](docs/integration-testing.md#remaining-release-qualification)，并在目标环境验证。

## 参与贡献

欢迎通过 [Issue](https://github.com/God1007/EvoAgent/issues) 反馈问题，或提交 PR。开发环境、检查命令和提交约定见 [贡献指南](CONTRIBUTING.md)。

## License

[MIT](LICENSE)
