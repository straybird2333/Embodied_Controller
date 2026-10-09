# RoboICL 远程仿真接口实施方案

## 目标

让远程机器上的 agent/模型能够驱动当前机器上的 RoboICL + Isaac Sim：远程端提交任务；当前机器运行仿真；仿真把观测送回远程端；远程端生成下一步策略；仿真执行并持续返回结果。对外使用独立的访问 API key。模型供应商的 `ASTRA_API_KEY` 只保存在运行模型的机器上，绝不下发给仿真客户端或 API 调用者。

目标是先支持一个仿真任务/一个会话的可靠闭环，之后再扩展并发、排队和多用户。首个版本应支持公网 HTTPS/WSS；公网可达性取决于当前机器是否有公网入口。若处于 CGNAT/无入站端口环境，需由仿真机主动连到有公网地址的 relay/tunnel 或远程端，不能靠绑定 `0.0.0.0` 绕过网络限制。

## 当前代码事实

- `roboicl/run.py` 目前在同一台机器启动 policy server 和 simulator client；WebSocket 端口绑定到 `127.0.0.1`，启动探测也只探测 loopback。
- `roboicl/policy_server.py` 通过 XPolicyLab 的 WebSocket `PolicyServer` 暴露 RoboICL policy，默认监听 `127.0.0.1`，接收 RoboDojo 的 RPC 调用。
- `roboicl/robodojo.py` 创建 `WsModelClient`，并把 `policy_server_url` 配置为 `ws://{host}:{port}`。
- `roboicl/rollout.py` 已有逐步交互流程：reset、发送观察、获取动作、执行动作、回传新观察，直到 episode 完成。该流程可以复用为远程闭环，不应再造一套机器人动作/观测协议。
- 当前 runner 将两边的 Python 依赖隔离在 simulator/policy 两个解释器中；远程模式应保留这个边界。
- 当前代码需要 `ASTRA_API_KEY` 和 `ASTRA_ENDPOINT` 才能进行模型推理。它们应由 policy 侧通过 secret/environment 注入。
- `roboicl/policy_server.py` 不是现成的公网服务：目前没有看到访问 token、用户/会话 API 或公网传输加密配置。

## 建议架构

```text
远程 agent / 模型机                          当前 GPU 仿真机
┌──────────────────────────┐               ┌─────────────────────────┐
│ Agent                    │  HTTPS + key  │ RoboICL Control API     │
│ ├─ 创建/查询/取消 run    ├──────────────>│ ├─ 校验请求和配额       │
│ └─ 模型策略/ASTRA_API_KEY│               │ ├─ 启动/管理 sim worker │
│                          │               │ └─ 状态、日志、结果     │
│ RoboICL policy worker    │<==== WSS =====│ RoboDojo WsModelClient  │
│ 观察 -> 模型 -> 动作      │  持久闭环通道  │ Isaac Sim + RoboDojo    │
└──────────────────────────┘               └─────────────────────────┘
```

控制面与策略数据面分开：

1. **控制面**：远程 agent 用 HTTPS API 创建、查询、取消任务和读取结果。API key 用于访问控制面。
2. **策略数据面**：仿真端与远程 policy worker 保持一个会话级 WSS 连接，沿用现有 XPolicy/RoboDojo RPC 消息语义，传输 observation/action/reset/finalize。连接应由仿真端主动建立，便于仿真机位于 NAT 后；使用 TLS 和独立会话凭证。若网络只能访问统一入口，则在入口做 WSS 路由/relay。
3. **模型调用**：policy worker 在远程模型机调用 Astra 或后续兼容 provider。模型 API key 只存在该 worker 的 secret 环境中。

服务边界建议：在当前机器新增轻量 `roboicl.api`/`roboicl.service` 控制 API 和进程管理器；不要在 Web handler 线程中导入 Isaac Sim，也不要让 API server 与 simulator 共用 Python 环境。每个 run 由独立 simulator 子进程执行，继续复用 `roboicl.run` 的 preflight、GPU slot 锁、日志目录、run manifest 和 RoboDojo 原生 evaluator。

## 外部 API 草案

所有接口要求 HTTPS。调用格式：`Authorization: Bearer <ROBOICL_ACCESS_KEY>`。key 由管理员在服务端配置/轮换，不写入仓库、日志、run manifest 或 URL。请求和响应使用 JSON。

| 方法与路径 | 用途 | 成功响应要点 |
|---|---|---|
| `GET /v1/health` | 存活与就绪状态 | API 版本、sim 依赖/资产是否就绪、可用 GPU slot；不得返回密钥或环境变量 |
| `POST /v1/runs` | 创建 run | `run_id`、`status=queued\|starting`、状态查询地址 |
| `GET /v1/runs/{run_id}` | 查询进度 | 状态、阶段、创建/更新时间、已完成步数（若可可靠采集）、错误摘要 |
| `POST /v1/runs/{run_id}/cancel` | 请求取消 | `cancel_requested`；管理器向子进程组发温和信号，超时后再强制终止 |
| `GET /v1/runs/{run_id}/result` | 取结构化结果 | 完成后返回 evaluator 的 `result.json` 摘要及受控下载链接/路径 |
| `GET /v1/runs/{run_id}/events`（可选） | 流式进度 | SSE；首版可以只提供轮询 |

创建请求字段建议：

```json
{
  "task": "deposit_coin",
  "shots": 0,
  "seed": 0,
  "layout": 0,
  "profile": "zero_shot_b25",
  "reasoning_effort": "xhigh",
  "policy_session": "由远程策略服务创建/分配的短期会话标识"
}
```

只允许仓库已登记的 task/profile 和受限数值范围；客户端不能传 shell 命令、任意文件路径、Python 路径、任意 profile URL 或任意可执行参数。`policy_session` 只引用已认证的短期会话；其 ticket 不可复用，避免把用户 API key 直接转成无期限 WSS 凭证。错误响应使用稳定错误码，例如 `invalid_request`、`assets_not_ready`、`gpu_busy`、`policy_unreachable`、`run_failed`。

run 状态至少包括：`queued`、`starting`、`running`、`cancelling`、`succeeded`、`failed`、`cancelled`。每个 run 绑定唯一目录和 session；完成/失败需保存退出码、错误类别、结果文件和关键日志引用。API 响应不得暴露服务器绝对路径、堆栈中的 secret 或任意本机文件。

## 远程 policy 会话协议

- 尽可能保留现有 `WsModelClient` 与 policy server 的 RPC 方法及参数形状，先检查 pinned XPolicyLab 客户端是否支持自定义 URL、TLS、认证 header/子协议和断线处理。
- 如现有传输无法安全认证，不要直接把现有无认证 WebSocket 端口开放到公网。优先增加一个 WSS gateway/relay，在握手阶段校验短期 session ticket；或者在可信反向代理/隧道内限制来源并做认证。
- 每个 policy 会话要绑定一个 `run_id`、task、seed 和过期时间。拒绝不匹配、过期、重复连接和越权会话。
- 观测数据含三路 RGB 与 proprioception；先沿用当前序列化格式。记录单条消息大小和传输延迟；必要时加上限、压缩/图像缩放和明确的拒绝错误，不能静默截断图像。
- 保留 rollout 中的动作验证、IK 连续性保护、controller feedback、episode 上限和 evaluator；网络掉线后不能继续执行旧动作队列，应停止/清空 pending action 并将 run 标为失败或可恢复暂停。
- 远端推理超时、重连、取消、session 清理均要有上限；不要无限等待或自动重放可能已经执行过的动作。

## 安全与运维要求

- **访问 key**：独立于 `ASTRA_API_KEY`；服务端读取 `ROBOICL_ACCESS_KEY` 或 key 文件，至少支持轮换/撤销。比较使用常数时间函数；日志中将 Authorization、query string 凭证和 secret 全部脱敏。
- **传输**：公网只开放 HTTPS/WSS，由 Caddy/Nginx/托管网关终止 TLS；应用本身只监听 loopback 或私网。设请求体上限、连接/读取超时、并发和任务频率限制。
- **最小权限**：API 进程使用普通用户；只管理允许的 runner 子进程；不提供通用 shell、文件浏览或任意目录下载接口。
- **资源保护**：复用 GPU slot 锁；首版建议全局最多一个仿真 run，其他请求排队或返回 `gpu_busy`。增加单任务最长时长、最大步数/输出体积和磁盘保留策略。
- **网络位置**：优先让仿真机主动连到远程 policy 服务/受控 relay，这样普通 NAT 通常无需入站端口。若确需对公网提供控制 API，在有防火墙控制的主机/反向代理上开放 443；不直接暴露 Isaac Sim、Kit、SSH 或 RoboDojo 原始端口。
- **审计**：记录 run_id、调用主体标识（不可记录原始 key）、时间、task、状态、资源使用和错误类别。日志不可含模型 key、访问 key 或完整敏感 prompt，默认设置结果保留期限。

## 分阶段实施

### 阶段 0：确认单机基线

1. 完成当前安装流程的资产 manifest、README dry-run 和一次不调用模型的相机 capture-only 检查。
2. 记录 Isaac Sim/RoboDojo 启动命令、输出目录、GPU slot 行为及一条真实模型请求的环境变量要求。
3. 若单机 capture-only 未通过，先修复安装；不在 API 层掩盖基础环境错误。

### 阶段 1：拆分 policy/server 与 simulator/client

1. 给 `roboicl.run` 增加显式 remote-policy 模式（例如 `--policy-url wss://...`）；该模式不在本机启动 policy server，而是在本机启动 simulator client 并连接配置的远端 session。
2. 保持现有同机默认流程兼容。remote 模式下校验 URL scheme、host、TLS（开发环境可显式允许 loopback `ws://`），不接受任意命令参数。
3. 在用户文档中说明：sim 主机只需要 simulator 依赖和 RoboICL 代码/资产；远端 policy 主机需要 policy 依赖与 provider 凭证。
4. 明确确认 RoboDojo `WsModelClient` 是否能设置 header/token 和 `wss://`。若不能，在客户端/传输适配层实现认证能力或用 gateway，不允许无认证公网 WS。

### 阶段 2：仿真机控制 API 和进程管理

1. 新增独立 API entrypoint，提供上表中的健康检查、创建/查询/取消 run、获取结果。
2. API 层采用轻量 Web 框架（agent 实施时确认仓库现有依赖及版本再决定，不把 Web 依赖塞进 Isaac Sim 环境）；锁定依赖版本。
3. 建立 run registry（首版可用 SQLite，重启后能恢复最终状态并识别孤儿进程）；安全启动 `roboicl.run` 子进程组并收集 stdout/stderr。
4. 使用固定 allowlist 将 API 字段映射为 CLI 参数；实现队列、GPU slot、超时、取消和结果归档。
5. 做优雅关机和服务重启恢复：服务重启不能误报仍运行的旧进程为成功。

### 阶段 3：公网与 agent 接入

1. 部署 HTTPS 反向代理/TLS；配置访问 key、速率限制、防火墙与审计日志。
2. 部署 WSS policy gateway/relay 或可信隧道；模拟端主动建链，逐 session 认证并隔离消息。
3. 提供 OpenAPI 文档、`curl` 示例和 agent tool schema；不得将真实 API key 写入示例、仓库或 shell history。
4. 若当前网络无公网入口，提供基于 outbound tunnel/relay 的部署说明；用户侧始终通过 HTTPS API 调用，无需 VPN。

## 验收标准

- 单机原有命令和默认 localhost 模式保持可用。
- 外部客户端只凭独立访问 key 可创建 run、轮询状态、取消 run 并读取最终结构化结果；无 key/错误 key 返回 401，越界输入返回 4xx。
- 在仿真机与远程 policy worker 分处两台机器时，可以完成至少一个端到端 rollout；远程端收到观察并返回动作，模型 key 仅存在远程 policy 机。
- session 与 run 一一绑定；跨 run 的观察/动作不能串线；掉线/超时/取消不会继续执行待执行动作。
- 公网连接仅使用 HTTPS/WSS；不存在未认证的公开 policy WebSocket、任意命令执行、任意路径读取或未授权结果访问。
- 完成/失败/取消状态准确，结果与日志落入 run 专属目录，敏感 key 不出现在进程参数、manifest、日志、API 响应和仓库文件中。
- 提供本机回环集成验证、无效凭证/输入验证和远程两机连通性验证的执行步骤及结果。验证只使用短任务；除非用户另行指定，不开展长时间批量评测。

## 给后续实现 agent 的工作约束

1. 先检查当前部署是否已经完成（资产 manifest、dry-run、capture-only），并报告实际状态，再实施 API。
2. 开始修改前复核 RoboDojo/XPolicyLab pinned submodule 的 WebSocket API 与 license；不要修改 pinned third-party 子模块，除非确认无法从 RoboICL 自有适配层解决并说明原因。
3. 保持默认单机执行兼容，所有公网监听都必须显式配置且默认关闭；开发模式默认仅监听 `127.0.0.1`。
4. 不要求用户把真实 Astra/model key 或公网 access key 发到聊天里；通过本机 secret 环境/文件配置。
5. 将服务使用、网络拓扑、密钥生成/轮换、systemd 部署和 tunnel/proxy 配置写入独立运维文档；仓库只放 `.env.example` 占位符，不放真密钥。
6. 按阶段提交实现，并在交付时列出修改文件、运行命令、验证结果、已知限制与公网部署尚需用户提供的信息（域名/网络入口等）。

