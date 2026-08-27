# 可插拔 Agent 与交接协议

## 1. 组装什么

`workflow.py` 提供与 PR 业务无关的有限 DAG 执行器。Agent 是“有版本的输入/输出契约 + 受信任的执行函数”，流程是数据连线，不再是一串写死的方法调用。

默认审查流程已经使用它，原有证据规则没有被替换：

```text
                  ┌─ critic ── critiques ──────┐
plan → specialists                            ├→ synthesize → fix → verify
                  └─ test ──── reproductions ──┘                  ↑
                               └─────────────────────────────────┘
```

支持替换任意内置角色、插入自定义 Agent、同一 Agent 多处复用、分支和多输入汇合；也可以定义与代码审查无关的流程。角色实现和流程连线可分别更换。

DAG 分支当前按确定顺序串行调度；`specialists` 内部保留原有有界并行池。分支表示数据依赖，不代表分布式或并行执行。环路、动态跳转、人审挂起和拖拽编辑器尚未实现。

## 2. 四个构件

| 构件 | 作用 |
| --- | --- |
| `PayloadType(name, version, validate)` | 定义一个 JSON 数据契约。版本必须精确匹配，validator 在发布和接收时都运行 |
| `AgentSpec(agent_id, revision, inputs, outputs, run)` | 可安装的 Agent。revision 是实现及行为配置的 SHA-256，不能用可变标签代替 |
| `Step(step_id, agent, sources)` | 将这个 Agent 实例的输入端口接到指定来源；同一 Agent 可实例化为不同 Step |
| `Workflow(name, inputs, steps, outputs)` | 校验连线与有向无环关系，调度节点并返回指定输出 |

端口来源只有两种：`$input.port`（流程输入）和 `upstream_step.output_port`（上游产物）。字段名可以不同，但源、目标契约的 `name@version` 必须相同。需要跨版本时，显式加入一个转换 Agent，不隐式转换。

编译后的 `Workflow` 与 `AgentSpec`、`Step` 一样被冻结，不能原地改写名称、节点、连线或 revision。需要修改时构造新的 Workflow（也可用 `dataclasses.replace`）；它会重新校验拓扑并计算 revision，防止定义与版本号分离。

validator 必须是纯校验函数：成功时返回，失败时抛出异常，不能修改 payload。执行器会拒绝校验期间的数据变化，保证幂等键中的输入摘要与实际交给 Agent 的数据一致；归一化或补默认值应由显式转换 Agent 完成。

`review-findings@1` 使用完整的 Finding 字段（包括 confidence），severity 必须是小写的 `critical/high/medium/low`。推荐直接使用 `Finding.to_dict()`；派生的 fingerprint 可省略，提供时必须与内容一致。未知字段、非法严重等级或错误指纹都在交接处拒绝，不能借用历史报告解析器的宽松降级规则悄悄改变含义。

节点只能返回声明过的输出字段。返回 `recipient`、`next_agent` 或额外状态字段不能改变流程；路由权属于执行器，不属于上游模型。所有引用、缺失输入、重复 Step、环路、类型版本冲突在执行任何 Agent 前被拒绝。

## 3. 关键：交接不是把聊天记录拼接给下一个 Agent

执行器向 Agent 传入 `Handoff`：

| 字段 | 用途 |
| --- | --- |
| `protocol_version` | 交接信封协议，当前为 1 |
| `task_id`、`workflow_revision` | 关联原始任务与固定的流程版本 |
| `step_id`、`agent_id`、`agent_revision` | 区分流程位置、角色身份与实现版本 |
| `sources` | 每个输入来自哪个上游端口，避免来源不明的状态混用 |
| `inputs` | 只含已连线且已验证的数据；与源对象深度隔离，不暴露全量共享状态 |
| `idempotency_key` | 稳定的本节点调用标识，可传给外部幂等接口 |
| `attempt` | 本节点尝试序号，首次为 1；接收校验失败也计入，不等同于 handler 调用次数 |
| `generation` | 当前 worker 的 admission fencing token；不是可由上游提升的权限 |
| `deadline`、`check_active()` | 本进程单调时钟截止时间与取消/generation 检查；不是跨机器时间戳 |

`inputs` 顶层不可赋值，嵌套值是该接收方自己的副本。Agent 修改自己的列表不会污染另一个分支，也不能改写已经持久化的上游结果。

大模型消息、PR 源码和工具结果仍是“不可信数据”，不能作为执行器指令。租户身份、仓库策略、模型出口权限仍从任务及服务端策略解析，不能因为上游在 payload 写了 `tenant_id` 或 `permissions` 就获得权限。

## 4. 发布、接收和恢复顺序

```text
固定流程/实现版本及原始输入摘要
  → 校验接收方输入与来源
  → 记录 running 与交接身份（若另一 worker 已完成，则直接接收其结果）
  → 调用 Agent（attempt + 稳定幂等键）
  → 校验输出端口、类型和 JSON 大小
  → PostgreSQL 原子提交本节点全部输出
  → 重新读取已提交的赢家结果
  → 下游再次验证输入后执行
```

一个节点的多个输出共同写入一个 checkpoint，不会只提交其中一部分。Join 等到全部父节点完成才运行；任一必需父节点失败，不发布“部分成功”的流程结果。

继续使用现有 `checkpoints` 表，不添加另一套状态存储：

- `workflow`：保存协议版本、流程 revision、应用执行 revision、原始输入 SHA-256，以及只包含契约/版本/连线的定义快照。
- `workflow:<step_id>`：调用前保存交接身份、输入 SHA-256、稳定幂等键、generation，状态为 `running`；成功后原子保存全部输出及输出 SHA-256，状态为 `completed`。校验/handler 失败则记录 `failed` 与不含异常正文的安全摘要。
- attempt/status 由原有表保存。节点恢复按名称读取自己的 checkpoint，不在每个节点反复传输整条流程的历史产物。
- checkpoint 里的交接身份还包含源端口连线和 Agent revision，可定位接收者究竟处理了哪一版输入。

恢复时先校验 manifest。原始输入、连线、契约版本、Agent 实现或应用 revision 变化，拒绝继续旧任务，应以新 revision 创建新任务；不会默默跳过旧步骤再拼接新结果。

外层 harness 复用 `executing` / `reviewing` 缓存时也必须通过同一份 manifest 校验，其中包含本次实际 Prompt 的摘要。校验发生在每次接收缓存时，包括另一 worker 晚到提交的结果，而不只在启动时检查一次。即使所有 Agent 都已完成、只是最终任务提交中断，也不能在回滚后把候选 Prompt 的旧结果当作稳定版结果提交；同版本、同输入的恢复仍不重复调用 Agent。已经持久化为 `SUCCESS` 的历史报告则直接读取，不算重新执行。

完成的节点从 checkpoint 恢复，不再调用 handler。失败的节点或进程中断留下的 `running` 节点重新执行，成功的父节点不重做。读取到的输出仍须通过类型及摘要校验，损坏的记录不会交给下游。

接收/输出契约、摘要或版本不匹配属于确定性的 `HandoffError`。外层 harness 不重试它；异步用例将它映射为永久投递失败，直接进入现有 DLQ 并释放 admission，不反复调用同一个有问题的插件。普通网络/运行时异常仍使用原来的有限重试。若并发任务已成功或已取消，仍优先接受该持久化终态，不能用晚到错误逆转成功。

并发 worker 的输出采用已有的 **first-write-wins**：即使后来的 worker 算出另一个结果，它也必须读取数据库中先提交的结果。失败写入不能覆盖已完成记录。取消和 admission generation 检查沿用现有 PostgreSQL 行锁与 fencing，旧 worker 的晚到结果不能提交。

注意两个不同保证：

- **产物交接**：下游只消费已提交、验证通过的输出。
- **handler 的外部副作用**：仍是 at-least-once。进程可能在外部操作成功后、checkpoint 提交前崩溃。对付款、发消息、写远端等操作必须使用 `handoff.idempotency_key` 或现有事务 Outbox；不能宣传任意 Agent 都 exactly-once。

幂等键绑定任务、流程、应用、Agent、原始输入和本次节点输入，不包含 attempt/generation，因此同一工作在重试或换代后仍使用相同键。不同任务不会共用键。

`AgentMessage` 继续作为审查解释日志，不充当交接完成凭据。日志可能在崩溃重试窗口重复；durable checkpoint 才是恢复依据。此次没有新增一套消息 ACK 协议。

## 5. 为什么暂时不用 Agent 间 RPC

同一个 worker 内，交接使用受限 JSON 数据的复制与函数调用；进程重启靠 PostgreSQL checkpoint 恢复。Redis Streams 继续分发整个任务，不为每条 DAG 边创建 topic/consumer group。

这样不会为七个本地角色引入网络超时、协议协商和重复投递的第二套复杂性。只有角色需要独立扩缩容、不同机器或不同语言运行时，才需要实现受信任的远程 handler；它仍应遵守同样的数据契约、超时、认证和幂等约束。当前执行器本身不是分布式工作流服务。

自定义 Python handler 是受信任的部署代码，不是沙箱。`deadline` 与 `check_active()` 是合作式约束；任意阻塞 Python 函数不能被安全强制终止。原有模型/网络 timeout、专用 reviewer 并发上限、Skill/Proof 容器隔离仍需保留。不可信插件必须继续使用现有 sandboxed Skill 路径。

## 6. 可直接运行的定制审查

```bash
python examples/custom_review_workflow.py
```

不需要 Docker、数据库或模型 Key。该示例保留所有默认证据门禁，然后追加一个自定义 `business-policy` Agent，并把最终输出接到该 Agent。它演示定义新角色、固定实现/config 摘要、声明契约、连接上游和执行。

也可以直接编辑 [business-review.json](../examples/business-review.json) 的连线，然后用同一个入口运行：

```bash
# 只预检并输出契约、连线与流程 revision；不执行 Agent，不连接数据库或模型
python examples/custom_review_workflow.py --workflow examples/business-review.json --check

# 用配置的流程审查内置示例 Diff，无需 Docker
python examples/custom_review_workflow.py --workflow examples/business-review.json

# 用相同配置启动完整 API；须先配置 PostgreSQL、执行迁移
python examples/custom_review_workflow.py --workflow examples/business-review.json --serve
```

如果使用 wheel 安装而不是 Git 工作区，两个示例文件会安装到当前 Python 环境的 `share/evoagent/examples`。在安装项目的同一虚拟环境中定位并运行，不需要切回源码目录：

```bash
example_dir="$(python -c 'import sysconfig; print(sysconfig.get_path("data") + "/share/evoagent/examples")')"
python "$example_dir/custom_review_workflow.py" --workflow "$example_dir/business-review.json" --check
python "$example_dir/custom_review_workflow.py" --workflow "$example_dir/business-review.json"
```

源码包也包含这两个文件。它们是可执行的安装资源，不是名为 `examples` 的可导入 Python 包；需要扩展时复制到自己的受信任部署模块，随应用发布。不要在运行中的安装目录里改写实现或配置；默认服务入口仍使用内置流程。

文件完整列出八个节点及分支/汇合，不使用隐含连线。`id` 是节点实例名，`agent` 是已注册实现名，`sources` 将每个输入接到 `$input` 或另一个节点的输出，`outputs.verified` 选择流程最终产物。可重新排序节点声明，实际执行顺序由依赖关系决定；改名或插入节点时要同步修改相关引用。

此入口复用七个内置角色，并注册 `business-policy`。增加新实现时，在自己的部署模块中定义 `AgentSpec` 并加入传给 `Workflow.from_dict` 的 catalog，不需要修改 EvoAgent 核心。配置不能提供 `module:function` 或从 PR 中自动加载插件。它是受信任的部署配置，不是让提交者指定审查流程的入口。

`--workflow` 接受严格 JSON，拒绝重复字段、非标准数值、非对象根节点和超过 8 MiB 的文件。缺失端口、未知 Agent、环路和类型不兼容在服务启动前拒绝；`--check` 只证明结构与契约有效，不代表通过了安全评测、生产资格或外部依赖检查。自定义连线仍需保留适用门禁并重新验证。

文件只在入口启动时读取一次。重建 stable/canary/shadow 的审查器时复用这份定义快照，但重新绑定当前 catalog 中的 handler，不会缓存旧 prompt 的执行函数。修改文件须重新启动并验证新 revision；运行中的 worker 不热加载它。

启动完整服务可直接使用同一个示例：

```bash
python examples/custom_review_workflow.py --serve
```

`--serve` 使用正常的 `EVOAGENT_*` 部署配置，须先配置 PostgreSQL 并执行现有迁移；不再是离线演示。它沿用原有 HTTP、鉴权、队列、Outbox 与关停流程，不另建一套服务器。生产镜像需要包含你的自定义模块并以它作为启动入口；默认 `python -m evoagent` 仍运行内置流程。

受信任的部署入口只需传入工厂：

```python
from evoagent.api import run
from examples.custom_review_workflow import build_workflow

run(workflow_factory=build_workflow)
```

嵌入其他宿主时，也可直接调用 `ReviewService(settings, workflow_factory=build_workflow)`，但宿主必须在退出时调用 `service.close()`。

`run()` 和 `ReviewService` 都可同时接收 `reviewer_contributions`（`ReviewerContribution` 序列）；传入时替换默认 security/reliability contributions，用于定制 specialists。受配置启用的 LLM 和磁盘 Skill 仍由原有 registry 装载。没有自动扫描/import 外部包，也没有从 PR 内容选择实现。

工厂拿到七个内置 AgentSpec：`planner`、`specialists`、`critic`、`test`、`synthesizer`、`fix`、`verifier`。例如替换 critic 并保留默认连线：

```python
from dataclasses import replace
from evoagent.agents import review_workflow


def build_workflow(catalog):
    custom = replace(
        catalog["critic"],
        agent_id="business-critic",
        revision=YOUR_IMMUTABLE_IMPLEMENTATION_AND_CONFIG_SHA256,
        run=your_critic_handler,
    )
    return review_workflow({**catalog, "critic": custom})
```

所有输入都是 JSON 形状；内置 adapters 负责转回 `ParsedDiff`、`Finding` 等业务类型。可复用 `FINDINGS_TYPE` 和 `REVIEW_INPUTS`，避免重新发明同名却不一致的契约。

ReviewService 的流程必须接受 `diff`/`parsed` 并返回 `verified` findings，保证 HTTP/report 边界兼容；通用 `Workflow` 不受这一业务限制。修复证明、GitHub 发布授权和 kill switch 仍由原有用例负责，不能靠改流程连线绕过。

## 7. 用配置拼装

`Workflow.from_dict(definition, agents, inputs)` 接受如下数据形状，可由受信任的 JSON 或 TOML 配置解析而来：

```json
{
  "name": "text-processing",
  "steps": [
    {"id": "normalize", "agent": "normalize-text", "sources": {"text": "$input.text"}},
    {"id": "count", "agent": "count-text", "sources": {"text": "normalize.text"}}
  ],
  "outputs": {"length": "count.length"}
}
```

这里的两个实现须先由部署代码注册到 `agents`，并声明匹配的 `text`/`length` 契约。配置只是选择已安装 Agent 和连线，不能传 `module:function` 来执行任意导入。完整可运行的配置分支/汇合示例也在 `tests/test_workflow.py`。

`workflow.describe()` 返回带协议/端口/实现摘要的审计 manifest；它不是 `from_dict` 的连线配置格式。`workflow.revision` 会参与生产 `reviewer_revision`，worker 原有版本检查继续生效。评测、stable、canary 和 shadow 都使用同一个 workflow factory；shadow/evaluation 不向主任务写 checkpoint。

工厂会在构建不同 prompt 版本的审查器时再次调用，必须返回与启动时相同的流程/Agent revision。执行器会在调用任何节点之前拒绝版本漂移，避免给新流程套上旧应用 revision 或旧资格评测结果。不要在工厂内部每次读取可变配置来偷偷换图；配置应由部署入口先读取为快照，改图需要发布新进程/版本并重新验证。允许变化的是已受发布治理约束的 prompt 版本，不是流程连线和角色实现。

复用内置角色时，每次用当前传入的 `catalog` 构建流程；不要缓存绑定着旧审查器的 Workflow，否则新 prompt 仍可能调用旧 handler。冻结的是流程定义，不是任意 Python 函数闭包里的外部状态；自定义 handler 的行为配置仍须由部署方固定并纳入 Agent revision。

只有类型兼容不代表安全语义相同。替换关键门禁后仍必须跑项目评测和真实场景验证；示例选择追加更严格策略，默认生产流程本身未改变。

## 8. 查看自定义流程的交接状态

`GET /v1/tasks/<task-id>/workflow` 使用现有 `read` 权限，租户来自认证身份，不接受由请求选择另一个租户。未知任务和其他租户的任务都返回 404。

它用一次 PostgreSQL 查询返回同一快照内的任务状态、固定流程版本和逐节点进度；不查询或返回节点的源码、prompt、实际输入/输出正文，也不返回任意插件异常文本。响应中的 `inputs`/`outputs` 仅为端口到 `name@version` 的契约映射。

主要字段：

| 字段 | 含义 |
| --- | --- |
| `task_state` | 外层任务状态，需与节点状态一起判断 |
| `availability` | `recorded`：存在定义快照；`not_recorded`：尚未开始或旧任务没有该快照；`pruned`：产物已按保留策略清理 |
| `artifacts_pruned_at` | 产物清理时间；与普通 trace 日志清理时间分开，不误报旧任务为已清理 |
| `workflow` | 流程名、协议版本、流程与应用执行 revision；无定义时为 null |
| `steps[].status` | `pending`、`running`、`completed` 或 `failed` |
| `steps[].blocked_by` | pending 节点尚未完成的直接父节点；空列表不表示一定已有 worker 调度它 |
| `steps[].sources`、`agent_revision` | 输入连线和实现版本，能定位哪个 Agent 在处理哪条交接 |
| `steps[].attempt`、`generation`、`idempotency_key` | 关联重试和 worker 换代；缓存恢复仍展示原产物的写入信息 |
| `steps[].input_sha256`、`output_sha256` | 不含正文的产物摘要；尚未提交输出时后者为 null |
| `steps[].updated_at`、`error` | 最后写入时间与安全错误摘要；可用错误引用关联服务端诊断 |

`running` 只表示已记录调度，不是独占租约或 worker 存活证明。取消、超时和硬崩溃可能留下这个状态；结合 `task_state`、队列健康及任务恢复机制判断，不应通过手工改 checkpoint 或直接调用插件来恢复。这里没有引入新心跳或第二套调度器。

## 9. 当前边界与验证

- 每个流程最多 64 节点，每组最多 64 端口；单份交接 JSON 上限 8 MiB，拒绝非字符串 object key、NaN/Infinity、NUL、非法 Unicode、Python 对象。
- 不支持循环、动态调度、运行时插件热加载、自动补偿和人审等待；不要用无限重试模拟这些能力。
- 外层 `ReviewHarness` 保留任务生命周期、总时限和有限重试。临时错误触发 executing 重试时，内部 DAG 会从未完成的 Agent 接续；契约/版本错误直接拒绝。
- 定制的实现摘要必须包含影响行为的 prompt/非敏感配置及外部 provider 代码版本。只填一个合法但永不更新的 SHA 会破坏版本保证。
- 运行 `python -m pytest -q tests/test_workflow.py tests/test_postgres_pool.py tests/test_admission.py`；配置 `EVOAGENT_TEST_POSTGRES_URL` 后会额外执行真实数据库的状态查询、失败恢复、保留清理、外层 harness 联合测试，以及自定义流程的 HTTP 提交/状态查询。CI 的真实基础设施契约 job 也运行 workflow 测试，并拒绝有 skipped 的结果；本地验证记录见 [integration-testing.md](integration-testing.md)。
- 同时配置独立测试用的 `EVOAGENT_TEST_REDIS_URL` 后，运行 `python -m pytest -q tests/test_workflow_delivery.py` 可验证跨进程交接：强制终止已有 running checkpoint 的 worker，由新服务实例从 Redis 接手；七个已完成节点的产物和协作日志保持不变，接收节点 attempt 从 1 增至 2，交接身份和幂等键不变，完成后 ACK 并释放 admission。另一个场景验证非法 Agent 输出仅执行一次即进入 DLQ，不生成报告。这不证明任意外部副作用 exactly-once，也不覆盖 Redis 服务端断电。
- 同一投递测试还覆盖队列状态丢失：停止 worker 后清除测试专用队列键，通过真实恢复 CLI 审核计划、确认摘要并重建 Outbox，再启动原版本流程。拒绝非空目标和错误摘要，同一恢复批次重复执行不新增投递；已完成交接不被重建命令改写。操作顺序见 [灾备手册](disaster-recovery.md)。这是逻辑队列丢失注入，不是 Redis 服务端断电测试。

这一步将原来“只能注册更多 Reviewer”的扩展面推进到“可替换角色 + 可组装数据流 + 可恢复交接”，没有把鉴权、事务边界和外部副作用规则交给插件自行决定。
