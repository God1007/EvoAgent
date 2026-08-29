<div align="center">

# EvoAgent

### 面向 Pull Request 的可组合代码审查 Agent 平台

定义 Agent，连接审查流程，让每一条结论都有来源。

[![CI](https://github.com/God1007/EvoAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/God1007/EvoAgent/actions/workflows/ci.yml)
[![Security](https://github.com/God1007/EvoAgent/actions/workflows/security.yml/badge.svg)](https://github.com/God1007/EvoAgent/actions/workflows/security.yml)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[组件模型](#组件模型) · [运行架构](#运行架构) · [Agent 内部设计](#agent-内部设计) · [实机演示](#实机演示) · [快速开始](#快速开始)

</div>

EvoAgent 将代码审查拆成可复用的 Agent 组件：安全检查、业务规则、模型分析、结果汇总。你可以直接使用默认流程，也可以在浏览器中定义组件、连接类型化端口，为不同仓库组装自己的审查流程。

输入一份 Unified Diff，或接入 GitHub PR 事件，EvoAgent 会执行选定流程，生成包含问题位置、风险等级、依据和修复建议的报告，并保留各节点的执行状态与交接记录。默认规则模式无需模型 API Key，适合先在本地体验，再逐步接入模型和 GitHub。

## 核心能力

| 能力 | 你可以做什么 |
| --- | --- |
| 可视化组装 | 在 Workflow Studio 中从配方或空白创建规则、结构化 Playbook 模型和汇总 Agent，像搭积木一样连接端口、试运行并发布版本 |
| 双轴审查 | 一键组装规范轴与需求轴并行检查，继续复用质询、复现、综合和验证门禁 |
| 定制审查标准 | 组合内置安全规则与自定义文本规则；配置 OpenAI-compatible 模型，补充语义分析 |
| 可读的审查结果 | 按风险查看问题，定位 Diff 新增行，阅读依据、修复建议和验证方法 |
| 可追踪的 Agent 交接 | 查看每个节点收到了什么、产出了什么；任务固定流程版本，支持失败后按检查点恢复 |
| GitHub 工作流 | 通过 GitHub App 接收 PR 事件、更新审查评论；满足验证条件的白名单修复可生成 Draft PR |
| 团队协作与运维 | 按租户隔离数据，以角色控制权限；在运维中心查看容量、审计和失败投递，并通过指标与链路追踪继续定位问题 |

## 组件模型

EvoAgent 的可插拔不是“把几段 Prompt 串起来”，而是把**能力、Agent、流程位置、数据交接和运行版本**分别建模：

> `Agent Skill[] + Playbook + 类型化端口 = AgentSpec`；`AgentSpec + Step + Handoff = Workflow`

| 构件 | 它固定什么 | 带来的能力 |
| --- | --- | --- |
| `PayloadType` | 端口数据的名称、版本与校验规则 | 只有契约精确匹配的组件才能连接；跨版本需要显式转换 Agent |
| `Agent Skill` | 一项带版本的证据工具或推理策略，以及它要求的输入类型 | 用户可以在 Studio 内勾选能力积木；缺少 Context、Evidence 或 findings 时不能发布 |
| `AgentSpec` | Agent 身份、Playbook、Skill 组合、输入输出端口、执行行为及不可变 revision | 规则、模型、汇总或受信任扩展都使用同一组件边界 |
| `Step` | 一个 Agent 在当前流程中的实例、名称和输入来源 | 同一组件可以多次复用，也可以只替换某个流程位置 |
| `Handoff` | 任务、流程、Agent、来源、输入、幂等键和执行代次 | 下游接收可验证产物，而不是来源不明的共享状态或聊天记录 |
| `Workflow` | Step、连线、流程输入输出和拓扑 | 发布前统一检查缺失输入、类型冲突和环路，并生成不可变版本 |

```mermaid
flowchart LR
    subgraph CONTROL[组装与发布]
        RECIPE[配方 / 空白画布] --> SKILLS[选择 Agent Skills]
        SKILLS --> SPEC[AgentSpec<br/>身份 · Playbook · Skills · 端口]
        SPEC --> STEP[Step 与连线]
        STEP --> CHECK[契约与 DAG 校验]
        CHECK --> VERSION[不可变 Workflow revision]
    end
    subgraph RUNTIME[执行与恢复]
        SNAP[固定 PR / Diff 快照] --> IN[类型化 Handoff]
        IN --> AGENT[当前 Agent]
        AGENT --> OUTPUT[校验后的产物]
        OUTPUT --> CP[PostgreSQL checkpoint]
        CP --> NEXT[下游 Handoff]
    end
    VERSION -->|任务绑定| SNAP
```

使用者的装配链路只有四步：选择配方或创建 Agent → 连接端口并通过校验 → 用真实 Diff 试运行 → 发布不可变版本并绑定仓库。每个任务再固定流程、Agent、执行环境和输入快照；之后修改草稿不会改变旧任务，失败恢复也不会悄悄换成“最新版”。

## 运行架构

EvoAgent 采用模块化单体架构：**Agent 负责局部判断，Workflow 负责装配与数据流，Harness 负责执行生命周期，应用层负责权限和外部操作。** 这些职责在同一服务内明确分开，增加或替换审查组件时，不必重新实现任务队列、认证、恢复或 GitHub 集成。

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

目前不把每个 Agent 部署成微服务：执行器按拓扑波次调度，每一波最多并行 4 个已经满足依赖的节点，汇合节点等待全部输入；默认 `specialists` 节点内部另有自己的有界并行池。未来若确实需要某类 Agent 独立扩缩容或跨语言运行，可在其执行函数后接远程服务，并继续遵守同一交接契约；当前并不提供分布式 DAG 调度。

## Agent 内部设计

在 EvoAgent 中，一个 Agent 是 **身份与版本 + 输入输出契约 + 执行函数**。执行函数可以是确定性规则，也可以调用模型；不是每个 Agent 都需要一段 Prompt 或一次 LLM 请求。

### 1. 身份：区分组件、流程位置和执行版本

| 标识 | 回答的问题 | 为什么需要 |
| --- | --- | --- |
| `agent_id` | 使用的是哪个 Agent 组件？ | 区分可复用的能力，不依赖界面上的展示名称 |
| `agent_revision` | 使用的是哪一版 Agent 定义？ | 以 SHA-256 摘要绑定组件实现 / 行为配置；Studio 从定义生成，Python 扩展由提供者绑定 |
| `step_id` | 这个组件位于流程中的哪个节点？ | 同一个 Agent 可以被多次复用，每个节点仍有独立的连线、状态和产物 |
| `task_id`、`workflow_revision` | 属于哪次任务、哪一版流程？ | 将执行和结果绑定到原始任务，防止跨任务、跨版本混用 |
| `execution_revision` | 本次执行使用哪套运行配置？ | 将应用源码、能力清单、模型配置和实际 Playbook / 旧版 Prompt 等纳入恢复校验，补足单个组件版本之外的依赖 |

身份不等于权限。用户与租户身份由服务端认证确定，模型出口策略从任务绑定的仓库策略解析；给 Agent 起名「管理员」，或在 Prompt、上游输出中写入 `permissions`，都不会获得额外权限。受信任的 Python 扩展则属于部署代码，不能把这种接口约束误认为代码沙箱。

`AgentSpec` 固定组件定义，`Step` 固定一次使用及其连线，`Workflow` 固定整张图。组件、节点和流程分开定义，才能做到「换一个检查实现」「复用同一个 Agent」「调整执行流程」互不混淆。

### 2. 上下文：按需传入，而不是共享全部历史

| 上下文 | 内容与来源 | 谁使用 |
| --- | --- | --- |
| 任务快照 | 原始 Diff；GitHub 任务还固定 base/head commit 与 Diff SHA-256；以及仓库与 PR 信息、仓库策略、选定流程和执行版本 | 应用层与恢复逻辑；不会整包自动传给每个 Agent |
| Context Pack | 来源、变更标题、Spec、项目规范和截断标记；手动提交或由 GitHub PR 生成，固定 SHA 归档可补齐仓库规范 | 只有显式连接 `review-context@1` 的 Agent；按不可信数据处理 |
| Repository Evidence Pack | 固定 GitHub head SHA、索引规模、变更文件/符号、受影响符号及引用文件；不含原始源码 | 只有显式连接 `repository-evidence@1` 的 GitHub PR 流程；手动 Diff 显示为不可用 |
| 节点输入 | `Handoff.inputs` 中明确连线的原始输入或上游产物；`sources` 记录真实来源 | 当前 Agent |
| 执行控制 | 当前任务、节点、尝试次数、幂等键、执行代次、截止时间 | 执行器与 Agent 的执行函数，不作为模型自行授予的权限 |
| 模型消息 | 按身份/目标/执行准则编译的 Playbook、所选 Skill 准则、服务端输出格式要求、已连线输入和证据 Skill 结果 | 通过受控模型网关发送给模型 |

例如，默认 `critic` 只接收解析后的变更与候选问题；`synthesize` 只接收候选问题、质疑结论和静态验证结果，不接收原始 Diff。需要什么由端口和连线声明，而不是让每个 Agent 从全局字典中自行找数据。

上下文管理包含三个具体约束：

- **数据隔离**：交接时通过 JSON 序列化生成独立副本。`inputs` 顶层只读，嵌套数据属于当前接收方；修改自己的列表不会污染上游或其他分支。
- **内存与长度控制**：执行器按最后使用位置释放不再需要的内存产物，数据库检查点仍按保留策略保存。单份交接数据上限为 8 MiB，这不是进程总内存上限；Context Pack 的标题为 512 UTF-8 字节、Spec/规范各 32 KiB，GitHub 正文或自动收集的仓库规范截断都会显式标记。Repository Evidence 默认最多下载并索引 32 MiB、5000 个 Python 文件，每类摘要最多 64 项；模型网关另行检查输入 token 估算、输出 token 与响应大小。
- **出站保护**：模型只收到当前请求构造的消息；凭据模式脱敏作用于出站副本，不改写保存的原始证据。脱敏不是完整 DLP，仍需遵守仓库数据策略。

这里的「上下文管理」是显式选择、隔离、限额和恢复，不是自动摘要或长期记忆系统。Repository Standards Pack 只读取固定名称和变更目录作用域内的规范文件；Repository Evidence 是 Python 静态影响摘要。二者都不是全仓库 RAG，也不证明动态调用关系完整。

### 3. 接口契约：先确认能接，再允许执行

Agent 的执行入口是 `run(handoff: Handoff) -> dict[str, Any]`。每个命名输入、输出端口都声明 `PayloadType(name, version, validate)`，例如 `unified-diff@1`、`review-context@1`、`repository-evidence@1`、`review-findings@1`。

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

### 6. 三层可插拔边界

| 边界 | 面向谁 | 如何扩展 | 不需要改什么 |
| --- | --- | --- | --- |
| Agent 内部能力 | 审查流程设计者 | 在 Studio 为模型 Agent 组合版本化 Agent Skills、Playbook 与类型化端口 | Agent 之间的连线和 Workflow 执行器 |
| 流程组件 | 审查流程设计者 | 将 Agent 加入画布，通过 Handoff 连接端口，或复制配方后重新连线 | 任务队列与 GitHub 接入 |
| 可执行审查插件 | 部署开发者 | 提供新的 `AgentSpec`，或以签名清单注册隔离运行的 Dynamic Skill | 其他 Agent 与流程协议 |
| 基础设施 | 平台维护者 | 在 `ports.py` 的窄接口后替换模型、代码托管、队列或验证执行器 | Agent 业务逻辑与流程定义 |

Studio 组件保存、试运行、发布不可变版本后再绑定仓库。Playbook 与模型、Skill ID/版本、端口一起进入 Agent 摘要；Context Pack 和实际需要的 Repository Evidence 进入任务快照。配方只复制成草稿，不形成可变运行依赖。

当前内置 8 个 Agent Skill：`local-rules`、`diff-summary` 提供确定性证据；`standards-alignment`、`requirement-alignment`、`repository-impact`、`regression-design` 提供上下文约束；`finding-critic`、`finding-synthesis` 处理上游结论。证据型 Skill 在模型调用前执行，推理型 Skill 由服务端编译为受信准则。两者都不能临时执行任意 Shell、导入 Python、扩权或改换下游 Agent；流程路由始终由 Workflow 控制。

这里的 **Agent Skill** 是模型 Agent 内部的能力积木；已有的 **Dynamic Skill** 是部署级、可执行的完整审查插件。前者由 Studio 组合，后者由运维安装并隔离运行，两者不共用权限边界。

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
| 模型服务 | 在规则检查之外增加语义分析，或运行自定义 Playbook 模型 Agent | [模型网关](docs/model-gateway.md)、[环境变量模板](.env.example) |
| GitHub App | PR 提交或更新后自动审查，并将报告回写评论 | [GitHub 接入](docs/api.md#github-接入) |
| 独立 Proof 执行器 | 在「修复验证」中编辑前后版本，查看复现、修复及回归证据；不写 GitHub | [执行器边界](docs/operations.md#dedicated-proof-executor)、[本机演示](docs/local-docker.md) |
| 修复验证容器 | 验证支持的确定性修复，再创建独立修复分支与 Draft PR | [修复运行说明](docs/operations.md#repair-outcomes)、[安全边界](docs/threat-model.md) |

自动修复需要真实 PR 快照、GitHub 权限和隔离验证环境；普通 Diff 审查不需要这些配置。

## 文档

| 想了解什么 | 从这里开始 |
| --- | --- |
| 看完整操作过程 | [实机演示](docs/demo.md) |
| 定义 Agent、连接流程、理解交接 | [Workflow Studio 与交接协议](docs/agent-workflows.md)、[自定义流程示例](examples/custom_review_workflow.py) |
| 集成自己的系统或 GitHub | [REST API 与 GitHub 接入](docs/api.md)、[模型网关](docs/model-gateway.md) |
| 理解架构和技术取舍 | [架构分层](#运行架构)、[Agent 内部设计](#agent-内部设计)、[模块说明](docs/architecture.md)、[设计决策记录](docs/adr/) |
| 部署、升级和排障 | [运行手册](docs/operations.md)、[macOS 独立 Docker](docs/local-docker.md)、[数据库迁移](docs/database-migrations.md)、[灾备恢复](docs/disaster-recovery.md) |
| 管理权限与安全策略 | 控制台「仓库治理」、[仓库策略](docs/repository-policies.md)、[威胁模型](docs/threat-model.md) |
| 查看测试与效果数据 | [集成验证](docs/integration-testing.md)、[评测方法](docs/evaluation.md)、[评测结果](docs/evaluation-baseline.md)、[性能测试](docs/performance.md) |

项目当前处于 Alpha 阶段。生产部署前请阅读 [待完成的验收项](docs/integration-testing.md#remaining-release-qualification)，并在目标环境验证。

## 参与贡献

欢迎通过 [Issue](https://github.com/God1007/EvoAgent/issues) 反馈问题，或提交 PR。开发环境、检查命令和提交约定见 [贡献指南](CONTRIBUTING.md)。

## License

[MIT](LICENSE)
