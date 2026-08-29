# macOS 独立 Docker 验收环境

这是 2026-08-28 配置的本地环境交接说明，不是一键安装器。通用部署方式仍见
[快速开始](../README.md#快速开始)；实测范围见 [集成验证](integration-testing.md)。

## 环境边界

使用 [Lima 官方 Docker 模板](https://lima-vm.io/docs/examples/containers/docker/)
建立独立的 `evoagent-docker` 虚拟机：Apple Virtualization、4 CPU、4 GiB 内存、
24 GiB 稀疏磁盘。`mounts: []`，不挂载宿主机目录、不自动传播代理环境，
不启动其他虚拟机，也不切换 Docker 的全局默认 context。

API、PostgreSQL 和 Redis 使用独立 Compose 项目 `evoagent-isolation`，
只向宿主机回环地址发布以下端口；已有的 8080/8081 服务不变。

| 服务 | 宿主机地址 | 用途 |
| --- | --- | --- |
| Web / API | `http://127.0.0.1:8082` | 登录、搭建流程、异步规则审查、修复验证 |
| PostgreSQL | `127.0.0.1:55435` | 应用库 `evoagent`；可清空的测试库 `evoagent_acceptance_test` |
| Redis | `127.0.0.1:56382` | 应用使用 DB 0；测试独占 DB 1 |

Docker 控制 socket 只供本机可信进程及独立 Proof 执行器使用，不挂入 Web 容器；不要分享 socket
或开放远程 Docker TCP 端口。API 需要访问数据库和队列，虚拟机也可以联网；
`--network none` 隔离的是运行 Skill 和不可信测试的短生命周期容器，
并不表示整个部署完全离线。

## 启动与登录

在项目根目录执行，以下配置文件已经保存在本机仓库之外：

```bash
runtime_dir="$HOME/Library/Application Support/EvoAgent-Docker"
limactl start -y evoagent-docker
source "$runtime_dir/app-env.sh"
docker compose --env-file /dev/null --project-name evoagent-isolation \
  -f docker-compose.yml -f docker-compose.proof.yml -f "$runtime_dir/compose.override.yml" \
  up -d --no-build --wait
```

打开 [Workflow Studio](http://127.0.0.1:8082/#studio)。初始用户名是 `admin`，
随机密码在本机 `$HOME/Library/Application Support/EvoAgent-Docker/admin-password`，
文件权限为 `0600`。改密使用 `POST /v1/auth/password`（当前没有页面入口）；
修改后的密码不会自动写回该文件。
管理员已持久化到 PostgreSQL，启动容器不再带初始化管理员密码。

已保留名为「Docker 实机验收：安全 + 业务 → 汇总」的发布流程及成功任务，
可直接查看三个 Agent 的定义、连线、报告和交接产物。它只处理本地示例 Diff，
没有创建 GitHub PR 或评论。

打开 [修复验证](http://127.0.0.1:8082/#proof)，点击「填入演示样例」并确认，再
点击「运行隔离验证」。样例检查一个边界值函数，实际执行修复前失败、修复后通过、
回归通过三个阶段。可直接修改文件，或添加/移除文件；不需要手写 JSON。
结果只覆盖这次输入与命令，编辑输入或退出登录会清除旧证据。

`env.sh` 只为当前 shell 设置独立 `DOCKER_HOST`、`DOCKER_CONFIG`，并清除
`DOCKER_CONTEXT` 覆盖。`app-env.sh` 再读取本机应用凭据。Compose 的私有覆盖文件
清除了仓库 `.env` 的注入；同时必须保留 `--env-file /dev/null`，避免插值读取它。
不要将这些私有配置、密码或数据库备份提交到 Git。

## 查看与停止

沿用上面的 shell 环境与 `runtime_dir`：

```bash
docker compose --env-file /dev/null --project-name evoagent-isolation \
  -f docker-compose.yml -f docker-compose.proof.yml -f "$runtime_dir/compose.override.yml" ps
curl -fsS http://127.0.0.1:8082/ready

docker compose --env-file /dev/null --project-name evoagent-isolation \
  -f docker-compose.yml -f docker-compose.proof.yml -f "$runtime_dir/compose.override.yml" stop
limactl stop evoagent-docker
```

停止不会删除数据库或工作流，下次按启动步骤恢复。没有配置 VM 开机自启。
PostgreSQL、Redis、Web/API 和 Proof 执行器都采用 `restart: always`，所以
Docker/VM 下次启动时会自动恢复，包括之前被手动停止的情况；迁移任务仍是一次性任务。
自动恢复不保证 Redis 最后一段 AOF 未丢失；若数据库还有非终态任务但队列已空，
保持停流并按[队列恢复手册](operations.md#queue-recovery)切换到空 Redis 目标。
若要持续禁用 Proof，应停止接收相关请求、只移除已停止的执行器容器，并在后续
启动时不叠加 Proof 文件；不要删除数据库卷。详见
[执行器恢复语义](operations.md#dedicated-proof-executor)。
不要使用 `down -v`、`docker system prune` 或删除 VM 来代替停止；这些操作可能丢失数据。

完成本轮回归后，实测 VM 目录约 5.5 GiB；另有 Docker CLI / Compose /
Buildx 合计约 127 MiB，不含既有 Lima 缓存和项目虚拟环境。24 GiB 是虚拟磁盘上限，
不是当前物理占用。VM 配额是 4 GiB 内存，空闲服务下 Linux 报告已用约 0.9 GiB；
镜像、日志、数据库及并发任务增长会增加占用。
临时压测和监控栈已清理；约 34 MiB 的压测备份和 621 KiB 的监控验收数据库备份
另存于验收目录，不计入 VM 大小。后者已恢复核对 600 条成功任务；监控时间序列
和告警记录也已保存。没有安装常驻 Prometheus；复现配置见
[监控运行手册](operations.md#monitoring-target-down)。
之后的故障验收使用另一台临时 VM，结束后已删除约 3.5 GiB 的 VM 数据；
其可重建配置、测试镜像包和报告另存在验收目录，主 VM 仍约 5.4 GiB。

## 重跑真实隔离测试

使用只设置 Docker 端点的 `env.sh`，不要把应用登录凭据加载到测试 shell：

```bash
source "$HOME/Library/Application Support/EvoAgent-Docker/env.sh"
export EVOAGENT_TEST_CONTAINER_IMAGE=evoagent:local-e11efb5f474b
.venv/bin/python -m pytest -q tests/test_container_integration.py
```

九项测试覆盖 Skill 源码快照、非 root/断网/只读边界、输出上限、超时清理、
启动确认丢失清理，以及本地适配器和 Unix socket 适配器的真实 L4 Proof。
其中两项子测试强杀各自的控制进程，确认两类容器按独立期限自行退出并被回收。
另一项将已过期的测试沙箱置于 pause 状态，确认下一项工作先按绝对期限清理它。
期限进程使用独立非 root 身份；边界检查还拒绝不可信代码的信号、调试和进程内存访问。
该镜像标签对应本轮工作树，不会随代码编辑自动更新；改代码后应重新构建，
并同步私有 Compose 覆盖文件和测试使用的镜像。

全量测试会清空测试表，必须使用上述独立测试库和 Redis DB 1，不能使用应用库：

```bash
export EVOAGENT_TEST_POSTGRES_URL='postgresql://evoagent:evoagent-local@127.0.0.1:55435/evoagent_acceptance_test'
export EVOAGENT_TEST_REDIS_URL='redis://127.0.0.1:56382/1'
export PATH="$HOME/Library/Application Support/EvoAgent/runtime/postgres/bin:$PATH"
.venv/bin/python -m pytest -q
```

最后一行 PATH 使用此前安装的 PostgreSQL 16 客户端；其他机器应换成自己的
`pg_dump` / `pg_restore` 路径。数据库用的是本地示例凭据，不适用于对外部署。

本轮全量回归实际执行 PostgreSQL 灾备恢复、Redis 和容器隔离路径，结果为
**961 tests、897 subtests passed，zero skipped**。

## 独立 Proof 的权限与升级

本机已叠加 `docker-compose.proof.yml`。Web 只挂载只读的私有 socket 卷，
执行器单独连接 VM 的 Docker socket，预载并固定沙箱镜像。当前 socket 组为
`987`，只适用于这台 VM；不要把这个数字照搬到其他主机。
执行器无网络、无应用密钥；测试容器既没有 Docker socket，也没有 Proof socket。
但执行器自身拥有 Docker 管理权限，`:ro` 不会把 Docker API 变成只读，
因此必须使用独立 VM/daemon。接口与资源限制见
[执行器运行手册](operations.md#dedicated-proof-executor)。

当前已部署的 Web/API、独立执行器和沙箱测试镜像分别是
`evoagent:local-e11efb5f474b`、`evoagent-proof:local-e11efb5f474b` 和
`evoagent:local-e11efb5f474b`。
升级时分别构建默认目标和
`--target proof-executor`，使用新标签，并同时更新私有覆盖文件和
`app-env.sh` 中的 `EVOAGENT_PROOF_IMAGE`。不要覆盖已经用于验收的旧标签。
API 启动时固定执行器的镜像 ID，所以更换沙箱镜像必须一起重启 API；
只更换执行器时，旧 API 会拒绝不匹配的回执。

已经验证执行器停止时返回 L1/无法判断，恢复后再次获得 L4，且原工作流和管理员仍在。
还验证了执行器被 SIGKILL 后容器独立到期回收；短期限故障演练结束后，已恢复
默认 120 秒执行期限。细节见 [强杀验收记录](integration-testing.md#sandbox-owner-loss-acceptance)。
另在全新的临时 VM 中完成了 Docker 守护进程 SIGKILL 和 VM 强制断电演练：
中断的 Proof 保持 L1，执行器恢复后新请求达到 L4，没有残留沙箱。它没有挂载
宿主目录，也没有复制本机应用数据；演练不影响 8082。记录及限制见
[故障验收](integration-testing.md#daemon-and-vm-fault-acceptance)。
普通 Compose 不叠加此文件时仍不具备 Proof 执行能力。这个适配器也没有启用
GitHub 自动修复或远程动态 Skill；真实模型/GitHub 写操作、多节点故障转移和
生产 SLO 仍未验收。完整应用数据的单 VM 断电、Redis 消息丢失识别、离线队列重建
与备份恢复已经验收，边界见[集成验证](integration-testing.md#full-application-vm-recovery)。
不要把本地 L4 样例当成整个项目的生产质量证明。
