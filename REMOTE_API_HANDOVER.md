# RoboICL 远程仿真 API 交接

更新时间：2026-10-12  
用途：在另一台机器上通过公网请求 RoboICL 仿真服务，并由外部策略客户端推送动作。

## 最新客户端接入

服务端已支持紧凑 float32 动作、分组反馈和服务端 JPEG。旧调用方式保持兼容；启用新格式须先检查会话协商回显。字段、回执及验证记录见 [流量精简协议](docs/lean-policy-transport.md)。

## 当前部署信息

- 公网 API：`https://drain-organizer-stewart-publisher.trycloudflare.com`
- 该地址当前经 Cloudflare Quick Tunnel 转发，属于临时地址；服务重启或隧道变化后可能改变。若连不上，先向服务器维护者确认最新 API 地址。
- 调用端不需要和仿真服务器处于同一网络，也不需要连接服务器 VPN。外部策略客户端主动连出到公网 WSS。
- 服务配置 8 张 GPU（`0`–`7`），最大并发数为 8；具体空闲卡以健康检查和 run 状态为准。
- 2026-10-09 已从 Mac 验证公网会话创建和外部 WSS token 鉴权成功。该验证没有启动仿真或调用模型；完整的外部策略驱动仿真尚未在此流程下端到端验证。

## 密钥说明

调用 API 需要服务器管理员单独提供 `ROBOICL_CLIENT_ACCESS_KEY`。它是**访问 RoboICL 服务的密钥**，不是 LLM provider key。不要把它写进本文件、代码仓库或聊天记录。

LLM provider key 由外部策略客户端自行持有，并只从那台机器调用相应模型服务；不需要安装在仿真服务器上。

公开交接文档使用密钥占位符。服务器本地私有副本位于 `.roboicl-api/REMOTE_API_HANDOVER.private.md`，权限为 600，未上传仓库。

在远程机器的终端设置：

```sh
export API='https://drain-organizer-stewart-publisher.trycloudflare.com'
export ROBOICL_CLIENT_ACCESS_KEY='<通过安全渠道取得的服务访问密钥>'
```

如果当前 shell 已安全设置了 `ROBOICL_CLIENT_ACCESS_KEY`，无需再次输入。结束操作后可运行 `unset ROBOICL_CLIENT_ACCESS_KEY` 清除当前 shell 中的变量。

## 检查公网 API

```sh
curl -fsS \
  -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  "$API/v1/health" | jq
```

健康时应返回 `"ready": true`，并显示可用 GPU 数量及 GPU 编号。如果没有 `jq`，去掉管道部分即可。

## 创建策略会话

会话包含短期、一次性的 `external_policy_token`。它授权外部策略客户端连接该会话的 WSS。把响应放入权限为 `600` 的临时文件，不要 `cat` 文件或公开 token：

```sh
umask 077
SESSION_FILE=$(mktemp)
curl -fsS \
  -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"task":"deposit_coin","shots":0,"seed":0,"layout":0,"profile":"roboicl-0shot-b25-v1"}' \
  "$API/v1/policy-sessions" > "$SESSION_FILE"

SESSION_ID=$(jq -r .policy_session "$SESSION_FILE")
printf '会话已创建：%s\n' "$SESSION_ID"
```

一旦 WSS token 验证成功，就不能再次使用该 token。不要先单独做 WSS 握手测试再用同一会话跑仿真；需要重新测试时，创建新会话。

## 运行外部策略客户端

调用流程的关键顺序：**先启动外部策略客户端，再提交仿真 run**。策略客户端主动连接 `external_policy_ws_url` 并等待仿真端；run 启动后，服务器把仿真观测经 WSS 发给客户端，客户端返回动作。

客户端需要 RoboICL 仓库中的 `roboicl.external_policy_client` helper，以及一个实现 RoboDojo 策略接口的 `MyPolicy`。`MyPolicy` 负责把观测交给你的模型/策略，并返回符合所选 profile 的动作。服务端不会替外部客户端调用模型，也不能通过普通 HTTP 请求直接提交任意动作。

在已安装 RoboICL 客户端依赖、且 `my_external_policy.py` 中有你自己的 `MyPolicy` 实现的机器上，运行：

```sh
python3 - "$SESSION_FILE" <<'PY'
import asyncio
import json
import sys
from pathlib import Path

from roboicl.external_policy_client import run_external_policy_session
from my_external_policy import MyPolicy

path = Path(sys.argv[1])
session = json.loads(path.read_text())
path.unlink()  # 删除包含一次性 token 的本地文件
asyncio.run(run_external_policy_session(
    session["external_policy_ws_url"],
    session["external_policy_token"],
    MyPolicy(),
))
PY
```

这条命令会保持运行，直到会话结束。不要在同一个 shell 前台运行时提交下面的请求；在另一个终端设置相同的 `API` 和 `ROBOICL_CLIENT_ACCESS_KEY`，并把刚才打印的会话 ID 设为 `SESSION_ID`，再提交 run：

```sh
export SESSION_ID='<刚才输出的会话 ID>'
```

若客户端与 API shell 不在同一台机器，需将会话 JSON 通过安全方式传给客户端机器，并限制文件权限为 `600`；不要经聊天或公开链接传输 token。

## 提交仿真任务

在已设置 `API` 和 `ROBOICL_CLIENT_ACCESS_KEY` 的终端中执行。`policy_session` 必须使用刚才创建的会话 ID，任务参数和策略 profile 要与创建会话时一致：

```sh
RUN_JSON=$(curl -fsS \
  -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"task\":\"deposit_coin\",\"shots\":0,\"seed\":0,\"layout\":0,\"profile\":\"roboicl-0shot-b25-v1\",\"policy_session\":\"$SESSION_ID\"}" \
  "$API/v1/runs")
printf '%s\n' "$RUN_JSON" | jq
RUN_ID=$(printf '%s' "$RUN_JSON" | jq -r .run_id)
```

响应中的 `run_id` 用来查询状态。任务开始后，`gpu` 字段会显示分配的 GPU：

```sh
curl -fsS \
  -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  "$API/v1/runs/$RUN_ID" | jq
```

任务完成后可取 evaluator 结果：

```sh
curl -fsS \
  -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  "$API/v1/runs/$RUN_ID/result" | jq
```

如果 run 长时间停在队列中，检查健康接口的 `available_gpu_slots`。如果策略客户端没连接、会话过期或策略回调报错，查看 run 状态中的 `stage`、`error_code` 和 `error_summary`。不要公开分享包含访问密钥或外部 token 的文件和命令输出。

## 调用流程概览

```text
远程机器                         RoboICL 服务器
  |                                  |
  |-- POST /v1/policy-sessions ----->| 创建会话和一次性 WSS token
  |<-- policy_session + WSS URL -----|
  |                                  |
  |-- WSS /external/{session} ------>| 外部策略客户端保持连接
  |-- POST /v1/runs ---------------->| 服务器分配 GPU 并启动仿真
  |<==== observations / actions ====>| WSS 转发仿真 RPC
  |-- GET /v1/runs/{id} ------------>| 查询状态与 GPU
  |-- GET /v1/runs/{id}/result ----->| 获取 evaluator 结果
```

## 相关文档

- [API 使用与协议说明](REMOTE_SIMULATION.md)
- [服务器部署记录](handover.md)
- [运维说明](docs/remote-simulation-ops.md)
- [OpenAPI 定义](docs/remote-simulation-openapi.yaml)
