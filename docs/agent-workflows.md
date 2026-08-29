# 可插拔 Agent 与交接协议

先看 [真实浏览器演示与五张操作截图](demo.md)，再按下面的步骤自行组装。

## 浏览器中搭建流程（Workflow Studio）

原来的 `AgentSpec` 和启动 JSON 面向部署开发者，不等于使用者能在产品里搭流程。现在可在侧栏「Agent 搭建」（`/#studio`）完成：**创建 Agent → 连线 → 保存草稿 → 真实试运行 → 查看交接 → 发布 → 发起审查或绑定仓库**。不要求用户改 Python、手写 JSON 或重启服务。「基础能力」仅展示已安装的 Skills，并提供搭建入口；它不是 Agent 编辑器。

首次升级先停止接收新任务并排空、停止旧 API/worker，备份数据库，运行 `python -m evoagent.migrate`，再启动新 API/worker。schema 26 增加定义、不可变版本和仓库绑定表；schema 27 将已有绑定固定到升级时有效的发布版本，并增加切换修订与版本外键。不能混用旧绑定逻辑和新逻辑，详见 [数据库升级](database-migrations.md)。保留原有任务、checkpoint、队列和 Outbox，不引入新的执行引擎。仅查看静态 HTML 不能保存或执行流程，需要正常运行的 API 与 PostgreSQL；本地规则体验不需要 Docker、Redis 或模型 Key。

### 十分钟体验：自己组装三个 Agent

1. 管理员登录后进入「Agent 搭建」，点击「＋ 规则检查」。创建「安全审查」，勾选动态代码执行和不安全 YAML 规则，点击「发布并加入画布」。
2. 同样创建「业务日志审查」规则 Agent，添加业务规则：匹配文本 `print(`，标识 `BIZ-LOG`，严重性 `medium`，填写原因、建议和验证方法，再发布并加入。这是你定义的行为，不是给内置 Agent 换一个名字。
3. 点击「＋ 结果汇总」，创建「报告汇总」，保留 `security`、`business` 两个「问题与修复建议」入口，发布并加入画布。
4. 已有 Agent 也可拖入画布或点击「添加」。节点自动按依赖分层排列；每个节点有独立标识，可复用同一个 Agent 多次。
5. 两个审查节点的「代码变更」接到「PR 输入 / 代码变更」；汇总节点的两个输入分别接到审查节点的「发现的问题」。可以点上游输出再点高亮输入，也可在右侧下拉框选择来源。类型不兼容或会形成循环的连线被拒绝，服务器仍会独立校验。
6. 选中汇总节点，点击「将此输出作为最终报告」，或在「最终结果来源」选择汇总节点。点击「校验交接」，然后保存草稿。
7. 在「试运行流程」中选择「当前草稿」，填入 `local/studio-demo` 和下面的 Diff，点击「保存并试运行草稿」。这是本地任务，不会发送 GitHub 评论。

```diff
--- a/app.py
+++ b/app.py
@@ -0,0 +1,3 @@
+eval(value)
+yaml.load(data)
+print(value)
```

8. 刷新结果，打开交接记录。页面先显示风险摘要和三个问题卡片，再显示处理过程。汇总输入的安全分支有两个问题、业务分支有一个问题，输出合并为三个问题。展开「查看处理内容与结果」，以可读形式查看数据库中的真实产物，不是流程图模拟数据。
9. 发布流程后，将试运行对象切换到「指定发布版本」，填入版本号，再用代表性 Diff 验证该版本。页面单独标明最近一次试运行的流程名称、发布版本和仓库；即使画布已经有新修改，也不会保存或执行那些修改。试运行成功只表示这份 Diff 执行完成，仍需核查报告，不代表生产质量评测或审批通过。
10. 发布后，在「用于 PR 审查」点击「选择发布版本并发起审查」，会带上当前流程的最新发布版本；也可以在「发起审查」的流程列表中选择已发布版本。这只影响本次审查。要让后续 PR 自动使用，在「仓库自动使用」填入 `local/studio-demo`、读取当前配置、指定已验证的版本，再确认切换。接收真实 PR 还需完成 GitHub App 安装、Webhook 和仓库授权，绑定流程不会自动完成 GitHub 接入。

```text
                 ┌→ 安全审查 ── findings ────┐
PR / Diff ────────┤                          ├→ 报告汇总 → 审查报告
                 └→ 业务日志审查 ─ findings ─┘
```

### 一键双轴审查模板

部署已配置模型路由时，组件库顶部提供「双轴 PR 审查」。点击后会把以下原生 Workflow 放入当前草稿，不创建第二套执行器：

```text
                                             ┌→ 规范轴审查 ─┐
Diff + Context Pack + Repository Evidence ───┤              ├→ 双轴汇总 → 质询 / 复现 → 综合 → 修复判断 → 验证
                                             └→ 需求轴审查 ─┘
```

- 规范轴使用 Context Pack 的 `standards`；缺失时只把少量通用代码坏味道作为判断项，不臆造仓库规则。
- 需求轴比较 `title`、`spec` 与新增代码；没有需求资料时返回空结果，不把模糊描述扩写成新要求。
- 两个轴都可显式接收 `repository-evidence@1`，用于查看变更符号、受影响 Python 符号和引用文件；它不扩大最终问题只能落在新增 Diff 行上的限制。
- 两个 Agent 并行执行、分别保存 checkpoint 和交接产物。汇总只为后续证据门禁提供统一 findings；任务中心仍能分别展开两个轴的原始判断。
- 模板仍是普通草稿，可以替换 Agent、修改连线、试运行、发布和绑定仓库。未配置模型时不展示模板，规则流程仍可使用。

手动审查和 Studio 试运行可以填写标题、需求与项目规范；GitHub Webhook 自动使用 PR 标题和正文。运行时统一形成 `review-context@1`：`origin/title/spec/standards/truncated`。API 内容超限直接拒绝；PR 正文超限时明确标记截断。若流程还显式需要 Repository Evidence，Worker 会复用固定 `head_sha` 的同一份归档补齐空白的 `standards`：读取根级 `AGENTS.md`、`CONTRIBUTING.md`、`CODING_STANDARDS.md`、`.github/CONTRIBUTING.md`，以及变更文件祖先目录中的 `AGENTS.md`，并以文件路径标题分段。其他目录、README 和任意文件名不会进入上下文；调用方显式提交的规范永不被覆盖。

Repository Standards Pack 与规范字段共用 32 KiB UTF-8 上限。超长文件、截断的多字节字符、非 UTF-8、NUL 或符号链接会被有界处理并设置 `truncated`，不把不完整资料伪装成完整上下文。仓库文件仍是 PR 提交者可控的不可信数据，只提供审查依据，不能改变 Playbook、端口、模型、工具、权限或流程连线。初始 API / Webhook Context 参与受理幂等指纹；归档补充结果在执行前与 Evidence 一次原子写入任务快照，再参与 Workflow 输入摘要和恢复校验，续跑不会重新读取仓库新版本。

Repository Evidence 完全由服务端生成，API 和页面不能提交或覆盖。只有固定了 GitHub `head_sha` 且选中流程实际把 `$input.evidence` 连给节点时，Worker 才下载该 SHA 的 zipball，在一次安全遍历中同时生成 Standards Pack，并用现有 CodeGraph 建立有界 Python 影响摘要后保存 `repository-evidence@1`。摘要只包含来源、状态、固定 revision、索引文件/字节数、变更路径/符号、受影响符号、引用文件和截断标记，不保存归档或源码；`indexed_bytes` 仍只统计 Python 索引。下载、解码或建图失败会保存明确的 `unavailable` 快照并继续基础 Diff / 已有 Context 审查；手动 Diff 同样显示不可用，不冒充仓库分析。快照参与 Workflow 输入摘要、节点幂等键和恢复校验，重试不会换用仓库新版本。

### 用户可以定义哪些行为

| 类型 | 用户配置 | 交接与执行边界 |
| --- | --- | --- |
| 规则 Agent | 选择已安装规则，添加字面文本业务规则、风险等级、原因与建议 | 输入 `diff`，输出 `findings`；只检查新增行，不执行用户正则或代码 |
| 模型 Agent | 名称、结构化 Playbook（身份、审查目标、执行准则）、已配置模型、输出 Token 上限、只读工具、命名输入/输出端口 | 可串接文本、整数、布尔、Context、Repository Evidence 和 findings；服务端另行生成输出格式契约，Playbook 不能把端口或权限改成文字约定。模型只能看到连入的输入；输出 findings 必须显式连接 diff，位置由代码检查 |
| 汇总 Agent | 自定义多个 findings 输入端口名称 | 全部输入就绪后合并去重；同一问题保留较高风险的完整判断，再交给下游 |
| 内置角色 | 从组件库添加规划、审查、规范轴、需求轴、双轴汇总、质询、复现判断、综合、修复判断和验证 | 复用现有契约与实现；端口必须精确匹配；双轴模型角色仅在路由已配置时可用 |

模型下拉框目前只展示部署方已配置的单个受治理路由，不接受用户提供任意 URL、API Key 或 Python 模块。未配置模型时，可先用规则和汇总跑通。模型调用仍受租户/仓库的 provider、model、region 策略及网关脱敏、输入/输出大小和网络出口约束。

Playbook 不是另一种可执行 Skill：它只是模型 Agent 定义中的三段受限文本。发布时，Playbook、模型、工具、端口和 Token 上限一起进入不可变版本与 SHA-256 摘要；任务再固定整个流程 bundle。服务端按固定顺序编译三段文本并追加类型化输出格式、输入不可信和 findings 位置约束。旧版 `prompt` 定义仍按原始结构与摘要执行，以保证已发布流程和失败任务可恢复；编辑旧草稿后发布的新版本会使用结构化 Playbook，不会改写旧版本。

组件库提供「规范与架构审查」「需求一致性审查」「回归与验证审查」三个 Agent 配方，分别吸收了 [`code-review` / `codebase-design`、Spec 轴、`tdd` / `diagnosing-bugs`](https://github.com/vinvcn/mattpocock-skills-zh-CN) 中适合 PR Review 的纪律。点击配方只会把经过服务端校验的定义复制到一个未保存草稿，所有字段仍可编辑；保存和发布继续走现有 Agent API。版本中不保存配方 ID，也不会在运行时读取远程 Markdown，因此配方更新不会改变历史版本、流程摘要或已固定任务。未配置模型时仍可先保存配方草稿，但不能发布或执行。

工作室的输入和工具结果虽封装为 JSON，模型出口仍会先解码再脱敏，避免引号转义绕过凭据规则。只修改发给模型的副本，不回写原 Diff、Agent 定义或交接产物；脱敏后还会再次检查输入预算。此能力不是完整 DLP，范围与限制见 [模型网关](model-gateway.md)。

只读工具目前是内置规则扫描、Diff 文件/行数摘要；勾选后在模型调用前执行，不是允许模型无限调用工具的自主循环。自定义 findings 的字段和新增行位置会被校验，但不代表发现必然正确，也不自动获得内置七阶段流程的全部证据门禁。发布者负责保留必要门禁并验证业务效果；任意 Shell、插件上传和外部副作用均未开放。

汇总不会以端口或输入列表的先后顺序裁决风险。同一文件、行号和问题指纹（规则、标题、归一化代码证据）重复时，优先保留 `critical → high → medium → low` 中较高等级的完整记录；等级相同时优先较高置信度，再用稳定的完整判断关联键打破平局，不拼接不同 Agent 的风险、建议和置信度。输出按风险、文件和位置等稳定排序。不同位置、标题或代码证据仍保留为不同问题；结果上限在去重后检查，超过 100 条会失败，不截断遗漏。各分支的原始判断仍可在「处理内容与结果」中核对，发布新版也不改写旧任务报告。

这是保守汇总，不是证明较高风险的判断一定正确；同等级同置信度时的关联键也不是质量评分。需要基于证据排除误报或降级时，应显式接入质询、复现判断和综合节点，并验证流程效果，不依赖重命名端口来覆盖结果。

### 草稿、发布、绑定和恢复

- 草稿允许临时不完整：空流程、待连接端口、未填完的业务规则和 Playbook 可以保存。保存时校验结构、字段类型和大小，补齐省略的编辑器字段；不接受 `steps: null`、未知配置字段、重复节点标识或错误类型，也不静默丢弃调用方传入的字段。保存携带上次 `revision`；多人同时编辑时冲突返回 409，不静默覆盖。
- 保存不代表可发布。发布仍要求完整规则或 Playbook 身份/目标和有效端口，并校验流程中 Agent 版本存在、连线类型匹配、无环和最终结果可达。Playbook 身份最多 200 字符、目标 2000、执行准则 12000；准则可为空。缺失的 Agent 引用在草稿画布中保留，可移除后重新添加；暂不可用的模型/连线选项保留当前值，不自动切换到下拉框第一项。
- 发布进行完整校验：端口、数据类型版本、缺失来源、重复标识、环路、最终 findings 输出；每个节点必须连接到最终结果，不能挂隐藏的模型调用分支。
- Agent 发布生成不可变 `vN`。流程节点固定引用 Agent 的某个版本；发布 Agent v2 **不会**自动修改流程中使用 v1 的节点。选中节点后可在「使用哪个 Agent」直接更换组件或版本：同名、同类型的输入与下游连线保留，不兼容的连接清空，需补齐并重新校验。节点标识与技术契约收在「高级」中。
- 流程发布时嵌入所引用的 Agent 定义，并存 SHA-256 摘要。**发布不等于上线**：仓库固定引用明确的流程版本，发布 v2 不会改变正在使用 v1 的仓库。画布展示的是当前草稿，不是仓库当前发布版本。
- 上线与回退使用同一个切换操作：先读取仓库当前配置，再指定已发布 `version`，携带读取到的 `revision`。提交前检查版本存在、摘要、契约及模型可用性；并发修改返回 409，页面要求重新读取，不自动覆盖。版本号可直接输入，回退不受最近 100 项版本提示的限制。恢复默认流程保留修订号，旧页面不能借由解绑重新覆盖配置。
- 任务接收时将完整流程 bundle 固定到输入快照，Webhook 任务同时固定经过校验的 `base_sha + head_sha`，而不是拉完 Diff 或 worker 开始时再读最新绑定/PR。Worker 通过固定 compare endpoint 取得 Diff 后保存内容和 SHA-256；后续 push、草稿修改、发布新版本或解除绑定都不改写旧任务。升级前没有 `base_sha` 的任务才使用原 URL 兼容路径。
- 切换自定义流程前还会复用任务接收的仓库策略，检查当前审查器、provider、model 和 region。不匹配返回 403，不改写绑定或记录成功切换；校验不执行 Agent、不调用模型、不创建任务。仓库仍启用时，即使模型策略暂不匹配，也可恢复默认流程。
- 这项预检只证明读取到的策略与当前运行配置兼容，不是质量评测、审批或未来可用性承诺。之后策略或部署配置仍可能变化，任务接收与执行继续独立校验；已有任务保留原快照。没有 Diff 的切换操作不预判后续变更大小、模型 Token 预算或审查质量。
- 草稿试运行先保存并固定 `draft_revision`；发布版试运行显式固定 `version`，不保存脏草稿，也不更改仓库绑定。读取发布版本失败会停止，不回退到当前草稿或最新版本。结果身份跟随已提交任务，不随当前表单变化；刷新后读取数据库中的任务快照，退出登录使尚未提交的过期请求失效。
- 两种试运行失败后均可点击「从失败节点重试」：沿用原 Diff、原版本和成功节点产物；不是拿当前草稿偷偷续跑。契约错误应修改草稿并提交新任务，取消的任务不能续跑。关闭页面后，可从任务中心重新打开持久化记录，并按当前权限取消执行或「从失败节点续跑」；试运行表单本身不跨页面重载保存。
- 取消是协作式的：请求先持久化取消标记，正在执行的 Agent 可能要等当前调用返回后才能退出。晚到结果不得提交为交接产物或报告，下游不会因此继续执行；已取消任务的重复队列投递只确认消费、不重跑审查。这不承诺强制中断任意 Python/模型调用，也不能撤销已发生的外部副作用。
- 如果提交时断网或等待 30 秒仍未收到确认，保持当前登录会话、仓库、Diff 和流程选择不变再提交，可找回原任务（原任务记录仍保留时）。手动审查和 Studio 各在当前标签页的会话存储中保留一条待确认摘要和随机标识，不存 Diff、仓库、Token 或流程正文；同一标签页重载后重新填入完全相同内容也可重试。已确认提交再次运行、改变输入/版本或重新登录均视为新提交，先到任务中心核对，避免重复启动。不会自动重试；退出登录还会清空表单并阻止旧响应回填。
- Agent 代码/应用执行版本变化后，旧任务可能因 revision 不匹配而拒绝续跑。这是安全拒绝，不表示产品具备跨应用版本执行旧 Python 实现的能力。
- 发布、保存和绑定写入现有审计日志；绑定切换与记录操作者、切换前后版本的审计在同一事务提交，审计失败则配置不变。定义和产物查询均按租户隔离。创建、编辑、发布、绑定、草稿试运行需要 `manage` 权限；普通审查权限可显式使用已发布版本，不改变仓库绑定。绑定是默认任务路由，不是限制所有显式试运行的版本白名单。
- Studio 自定义流程不计入默认 `llm-review` 的 canary/shadow 资格数据，避免不同流程的结果污染同一发布评估；目前没有 Studio 专属的灰度、审批或自动回滚系统。

### API 与当前限制

| 操作 | 接口 |
| --- | --- |
| 可用类型、模型、工具、内置角色、Agent 配方和流程模板 | `GET /v1/studio/catalog` |
| 列表、保存草稿 | `GET/POST /v1/studio/agents`、`/v1/studio/workflows` |
| 读取草稿及版本摘要 | `GET /v1/studio/{kind}/{id}` |
| 发布草稿 | `POST /v1/studio/{kind}/{id}/publish`，正文 `{"revision": 1}` |
| 读取不可变版本 | `GET /v1/studio/{kind}/{id}/versions/{version}` |
| 校验流程 | `POST /v1/studio/validate`，正文 `{"definition": ...}` |
| 切换/回退仓库版本 | `POST /v1/studio/binding`，正文 `{"repository":"owner/repo","workflow_id":"...","version":1,"revision":0}` |
| 查询绑定 | `GET /v1/studio/binding?repository=owner/repo` |
| 固定发布版本执行 | `POST /v1/reviews?async=true`，正常正文额外包含 `"workflow":{"id":"...","version":1}` |
| 固定草稿试运行 | 同上，选择改为 `"workflow":{"id":"...","draft_revision":1}` |
| 查看某次交接 | `GET /v1/tasks/{task}/workflow/{step}` |

`kind` 为 `agents` 或 `workflows`。保存正文为 `{"revision":0,"definition":...}`；更新时带 `id` 和当前 revision。异步审查原有 `Idempotency-Key` 仍可使用，显式 workflow 选择和 Context Pack 都参与请求指纹；同一请求重试返回原任务，不会因草稿已修改而切换版本。

新模型 Agent 的 `config` 形状为 `{"playbook":{"identity":"...","objective":"...","instructions":"..."},"model":"...","tools":[],"max_output_tokens":2048}`。端口仍位于 Agent 定义的 `inputs` / `outputs`，不复制进 Playbook。API 只为兼容已存在的不可变版本继续接受旧 `prompt` 形状；新调用方不应据此建立第二种编辑模型。

绑定查询从未配置时返回 `{"binding":null}`，首次切换携带 `revision:0`；已配置时返回 `workflow_id`、`version`、`revision`、发布时的名称 `name` 和更新时间。解除绑定需要同时传 `workflow_id:null`、`version:null`，并携带当前修订号；返回的解绑记录保留递增的 `revision`。下一次绑定必须使用该修订号，不能再传 0。POST 返回本次事务实际写入的配置；不省略版本或隐式选择最新版本。

组件和流程列表支持游标分页：`GET /v1/studio/{kind}?limit=100`，响应包含 `documents` 和 `next_cursor`。有下一页时，将返回的游标 URL 编码后作为 `cursor` 参数传回；`next_cursor:null` 表示已到末页。每页默认及最多 100 项，非法游标、页大小或重复分页参数返回 400。游标只表示排序位置，不授予权限，每次查询仍限定当前租户和资源种类。

工作室提供「加载更多 Agent」「加载更多流程」，不会保存、发布或改变当前连线。加载失败保留原列表与游标，可原位重试；加载成功后键盘焦点移到新组件或流程选择框。组件列表独立滚动，不把画布撑成超长页面。保存、切换或刷新会重读第一页；当前选中的旧流程及画布引用的旧 Agent 版本不会因此变成不可用。

翻页不是数据库快照：期间被编辑的条目可能移动到第一页，需刷新重新查看；新建条目也在刷新后出现。客户端按 ID 去重，不按位置偏移翻页。版本历史摘要仍最多返回最近 100 条，但可用明确版本号读取更早的不可变版本。

Studio 单份定义/发布 bundle 上限 256 KiB，最多 64 节点，自定义 Agent 每组最多 16 端口、32 条字面规则，单节点 findings 上限 100。当前没有名称搜索、删除、跨租户分享、任意 JSON Schema、自定义工具市场、条件分支、循环、人审挂起或画布自由定位。DAG 的就绪分支在单进程内有界并行；它不是跨进程的分布式工作流平台。

实际输入根据任务原始 Diff 和已提交上游输出重建；保留策略清理后会返回产物不存在或列出 `unavailable_inputs`，不伪造完整历史。完整诊断正文需要 `manage` 权限；普通账号可读取经过白名单过滤的页面视图，正文仍可能包含有权查看的源码。

用户界面只展示风险、问题位置、依据、建议和可读处理进度。页面请求携带 `X-EvoAgent-View: console`，服务端按字段白名单返回页面所需内容：任务和组件库响应不含 Prompt 配置、完整输入快照、指纹、generation、幂等键或 SHA 摘要；交接产物按已声明类型投影，未知类型不回退为原始 JSON。`review-context@1` 显示为来源、标题、需求和规范区块，截断状态明确提示，不展示附带 metadata 或原始任务 JSON。Agent 编辑器显式读取的用户定义仍包含可编辑的 Prompt 和端口。

展开交接输入时，标题显示任务固定连线对应的真实来源 Agent 和输出，另列接收端口的可读名称。端口叫 `security` 并不代表来源一定是安全审查 Agent；名称、来源和结果始终跟随该任务的快照，不使用当前画布或最新 Agent 定义猜测。

旧版省略字段的草稿仅在页面读取时补齐到副本，GET 不修改原记录或 revision。已存但结构错误/包含未知字段的草稿会显示“原始内容未修改”，不会崩溃后覆盖为空流程；原始诊断接口仍可读取，需通过 API 修正或升级到支持该结构的编辑器。草稿切换失败时保留当前编辑内容。

服务端在查询资源前检查权限，不能通过移除请求头获取更多数据：

| 数据 | 普通 `read` 账号 | `manage` 账号 |
| --- | --- | --- |
| 任务详情、流程状态、交接产物 | 必须使用 `console` 白名单视图 | 可读取原始诊断数据或页面视图 |
| Agent/流程的发布版本 | 仅 `console` 摘要，不含完整定义 | 可读取完整发布快照 |
| Agent/流程草稿正文、部署目录 | 不可读取 | 可读取并进入编辑器 |
| 定义列表、仓库绑定、Markdown 报告 | 可按原权限读取 | 可读取 |

`admin` 和 `platform_admin` 具有 `manage`，`maintainer` 和 `auditor` 没有。普通审查账号仍可显式运行已发布版本。旧客户端以普通账号读取原始诊断端点会收到 403，应改用页面视图或 Markdown 报告。权限来自当前服务端成员关系，降权后的旧令牌不保留管理员能力；管理员也不能跨租户读取。

这不是源码密钥检测，用户编写的文本仍可能包含敏感内容。没有页面视图的接口携带该头会在执行业务操作前返回 400，不默默退回原始数据；其他审计、运维接口仍按各自权限控制。

退出、收到登录失效响应或检测到其他标签页更换登录态，会清空当前页面的私有内容、未保存草稿和确认框，并使旧请求失效；不会将旧画布、报告或安装跳转带入新账号。重新登录后才可继续操作。已被服务器接收的保存、审查或发布不会因关闭页面或退出而自动撤销，操作结果应以重新读取的服务端记录为准。

验证：`python -m pytest -q tests/test_studio.py tests/test_console_view.py`（设置独立的 `EVOAGENT_TEST_POSTGRES_URL`）覆盖用户 Agent 执行、双分支汇总及顺序无关的风险保留、旧任务隔离、草稿形状与只读兼容、幂等、CAS、租户/权限、失败续跑及页面字段白名单；`node --test tests/web.test.cjs` 检查拓扑排列、节点改名/删除的连线更新、不可用选项保留、草稿/发布版试运行不混用、失败不降级、可读结果、丢失确认后的重试及登录切换隔离。`tests/test_admission.py` 另以真实 HTTP 断连验证数据库已提交后重试只保留一个任务和一条 Outbox 消息，两次请求仍分别审计。CI 的真实数据库契约任务包含 Studio 测试且不允许跳过。

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

DAG 按拓扑波次执行：同一波中已经满足依赖的节点最多并行 4 个，结果按稳定拓扑顺序并入执行上下文，汇合节点必须等全部来源完成。某一分支失败时，同波中已经提交的兄弟节点 checkpoint 会在续跑时直接复用。`specialists` 内部保留原有的独立并行池；这仍是单进程调度，不是远程 Agent、跨节点扩缩容或分布式 DAG。环路、动态跳转和人审挂起尚未实现。上面的 Studio 提供面向使用者的审查流程编辑；下面的 Python/启动文件路径则面向受信任的部署开发者。

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

执行器按固定拓扑计算产物的最后使用位置，只在内存中保留当前或后续节点、最终输出还需要的结果。已消费完的中间结果不随长链一直累积；分支、汇总以及直接引用原始输入的最终输出仍会保留所需数据。这里只释放运行内存，不删除数据库 checkpoint，历史交接查看和失败续跑不变。单次交接仍受 8 MiB 序列化上限约束；宽图汇总、校验副本和 Agent 自身分配仍占内存，不能把这个上限当作整个进程的内存配额。

交接大小按完整 JSON 的 UTF-8 字节计算，包含转义后的内容，不按字符数放行。编码前用保守下界限制结构遍历和明显超限的重复数据，再由标准库逐段编码，超过预算就拒绝，不先构造完整的超大 JSON。拒绝不会截断结果、保存部分输出或放行下游；合法数据保持原字节格式和摘要。上限仍不约束 Agent 内部预先分配的对象或编码器的单段临时内存。

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

数据库 schema v28 起，checkpoint 使用 PostgreSQL `JSON` 保留数字的序列化表示，避免 `JSONB` 把 `-0.0`、`1e20` 改写后造成摘要不一致。正常保存、重启与备份恢复都沿用原摘要规则，不归一化数字或重新计算历史摘要。旧记录中已经被改写的值无法由迁移还原；摘要不匹配仍拒绝接收，升级步骤与限制见 [数据库迁移说明](database-migrations.md)。

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

本节的启动文件只在入口启动时读取一次。重建 stable/canary/shadow 的审查器时复用这份定义快照，但重新绑定当前 catalog 中的 handler，不会缓存旧 prompt 的执行函数。修改部署文件须重新启动并验证新 revision；Studio 的数据库版本发布是独立入口，不受这个启动文件限制。

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

`GET /v1/tasks/<task-id>/workflow` 的原始诊断响应需要 `manage`；`read` 账号须携带 `X-EvoAgent-View: console` 读取精简视图。租户来自认证身份，不接受由请求选择另一个租户。获准使用对应视图后，未知任务和其他租户的任务都返回 404。

它用一次 PostgreSQL 查询返回同一快照内的任务状态、固定流程版本和逐节点进度；不查询或返回节点的源码、prompt、实际输入/输出正文，也不返回任意插件异常文本。响应中的 `inputs`/`outputs` 仅为端口到 `name@version` 的契约映射。

原始诊断响应的主要字段（页面视图会过滤实现摘要等内部字段）：

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
| `steps[].started_at`、`duration_ms` | 当前尝试开始时间，以及 Agent handler 与输出契约校验的耗时；不包含排队、上游等待或完成后的 checkpoint 写入 |
| `steps[].updated_at`、`error` | checkpoint 最后写入时间与安全错误摘要；可用错误引用关联服务端诊断 |

`running` 只表示已记录调度，不是独占租约或 worker 存活证明。取消、超时和硬崩溃可能留下这个状态；结合 `task_state`、队列健康及任务恢复机制判断，不应通过手工改 checkpoint 或直接调用插件来恢复。这里没有引入新心跳或第二套调度器。
旧 checkpoint、尚未执行的节点和未能提交结束状态的中断尝试可能没有
`duration_ms`；页面显示“耗时未知”比用更新时间推算更准确。重试成功后展示已提交的
最新尝试耗时，历史尝试次数仍由 `attempt` 保留，但不伪造累计执行时间。

## 9. 当前边界与验证

- 每个流程最多 64 节点，每组最多 64 端口；单份交接 JSON 上限 8 MiB，拒绝非字符串 object key、NaN/Infinity、NUL、非法 Unicode、Python 对象。
- 不支持循环、动态调度、运行时插件热加载、自动补偿和人审等待；不要用无限重试模拟这些能力。
- 外层 `ReviewHarness` 保留任务生命周期、总时限和有限重试。临时错误触发 executing 重试时，内部 DAG 会从未完成的 Agent 接续；契约/版本错误直接拒绝。
- 定制的实现摘要必须包含影响行为的 prompt/非敏感配置及外部 provider 代码版本。只填一个合法但永不更新的 SHA 会破坏版本保证。
- 运行 `python -m pytest -q tests/test_workflow.py tests/test_postgres_pool.py tests/test_admission.py`；配置 `EVOAGENT_TEST_POSTGRES_URL` 后会额外执行真实数据库的状态查询、失败恢复、保留清理、外层 harness 联合测试，以及自定义流程的 HTTP 提交/状态查询。CI 的真实基础设施契约 job 也运行 workflow 测试，并拒绝有 skipped 的结果；本地验证记录见 [integration-testing.md](integration-testing.md)。
- 同时配置独立测试用的 `EVOAGENT_TEST_REDIS_URL` 后，运行 `python -m pytest -q tests/test_workflow_delivery.py` 可验证跨进程交接：强制终止已有 running checkpoint 的 worker，由新服务实例从 Redis 接手；七个已完成节点的产物和协作日志保持不变，接收节点 attempt 从 1 增至 2，交接身份和幂等键不变，完成后 ACK 并释放 admission。另一个场景验证非法 Agent 输出仅执行一次即进入 DLQ，不生成报告。这不证明任意外部副作用 exactly-once，也不覆盖 Redis 服务端断电。
- 同一投递测试还覆盖队列状态丢失：停止 worker 后清除测试专用队列键，通过真实恢复 CLI 审核计划、确认摘要并重建 Outbox，再启动原版本流程。拒绝非空目标和错误摘要，同一恢复批次重复执行不新增投递；已完成交接不被重建命令改写。操作顺序见 [灾备手册](disaster-recovery.md)。这是逻辑队列丢失注入，不是 Redis 服务端断电测试。
- `tests/test_workflow_delivery.py` 也覆盖 Studio 自定义流程：在独立租户发布两个规则 Agent 和一个汇总 Agent，强制终止汇总节点所在进程，再发布并绑定不同的 Agent/流程 v2。另一个进程中的服务接管旧任务时，仅执行未完成的汇总；两个上游节点不重跑，原快照、交接身份与幂等键不变。新任务使用 v2，完成后 ACK 并释放 admission。该测试使用真实 PostgreSQL/Redis；故障注入只暂停汇总函数，不模拟存储、队列或规则输出。它不替代浏览器交互、真实模型调用或 GitHub 外部副作用验收。

这一步将原来“只能注册更多 Reviewer”的扩展面推进到“可替换角色 + 可组装数据流 + 可恢复交接”，没有把鉴权、事务边界和外部副作用规则交给插件自行决定。
