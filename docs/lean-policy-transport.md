# 公网 RTX 外发流量精简协议

更新：2026-10-12。实现位于本仓库 owned adapter，锁定子模块没有修改。
公网 API 地址、访问 key、WSS 鉴权和 RPC 包装沿用现有部署；本文不包含密钥。
AIHub 输入不属于本次限制范围，本服务不修改 LLM 提示词或输入。

## 1. 显式协商，旧客户端继续可用

`GET /v1/diagnostics/capabilities` 的 `policy_transport` 声明支持能力。
创建会话时选择所需的独立选项：

```json
{
  "task": "deposit_coin", "shots": 0, "seed": 0, "layout": 0,
  "profile": "roboicl-0shot-b25-v1",
  "action_format": "ee_targets_f32_v1",
  "feedback_mode": "chunk_boundary",
  "observation_encoding": "jpeg", "jpeg_quality": 90
}
```

请求地址仍为 `POST /v1/policy-sessions`。响应保留原来的 session、WSS URL、
一次性鉴权凭证，并回显这四项最终配置及 `transport_capabilities`。
客户端必须检查回显后才切换动作格式。发送动作前仍按原校验器生成绝对 float32 目标。

`POST /v1/runs` 可以省略这四项，自动继承所属会话；如果显式填写，必须一致，
否则返回 422 `invalid_request`，message 为 `policy_session_transport_mismatch`。
会话归属、任务参数、一次使用限制、过期限制继续生效。
配置存入数据库、run 状态、`run.json` 的 `policy_transport_options`，并绑定双方签名票据。
网关拒绝配对配置不同的客户端与仿真端。

默认配置为 `legacy / per_step / raw_rgb / null`，不会自动开启新表示或减少反馈。
JPEG quality 范围 1–100，选择 jpeg 时未填质量则为 90；raw_rgb 不接受非空质量。
可分别只开启紧凑动作、分组反馈或 JPEG，便于单独比较。

## 2. get_action 紧凑返回

继续保留 `message_id/request_id`、evaluation/trial 标识、RPC envelope 和
`payload.ok` 等现有字段，只替换 `payload.result`：

```python
{
    "format": "ee_targets_f32_v1",
    "base_step": 8,
    "targets": np.ndarray(shape=(H, 16), dtype=np.float32),
}
```

每行依次为：左 `x,y,z,qw,qx,qy,qz,gripper`，右同样八项。
传输仍使用现有 MessagePack numpy codec；不能把矩阵改成 JSON list、float16 或 Base64。
四字段恢复后的 float32 字节逐位一致，不额外归一化四元数、舍入或删除静止臂。

`base_step` 来自最近正式观测的 `astra_meta.step`，不是 RPC envelope 中的推理轮数。
服务端检查整数步数、dtype、H×16 shape、H 在 1–执行 horizon 且不超过 64、
有限值、wxyz 单位四元数（范数误差不超过 0.001）和 [0,1] 夹爪范围。
过期步数以 `stale_action_base_step` 拒绝；其他非法紧凑内容以
`invalid_action_payload` 拒绝，并终止该 run，禁止派发其中任一步。
chunk_boundary 模式先发送零执行回执与新观测，再停止；失败分类保存在服务端日志。

辅助代码：[compact_actions.py](../roboicl/compact_actions.py)。可供客户端复用或按相同契约实现：

```python
from roboicl.compact_actions import pack_actions
result = pack_actions(validated_float32_actions, base_step=latest_obs["astra_meta"]["step"])
# 将 result 放回原来的 RPC payload.result。
```

服务器只解包，然后进入现有 IK、执行、跟踪、夹爪及终止检查。
网关仍原样转发，不重写包；XPolicyLab 的 request_id 响应缓存不变。

## 3. chunk_boundary 反馈

初始观测仍通过 `update_obs` 发送并等待确认。每个正式动作仍在服务器执行，
每步仍获取本地观测、保存视频、检查 IK/跟踪和任务终止。
正常动作组结束发送一次最新三图与状态；中途 IK 拒绝、跟踪中断或任务终止立即发送边界反馈。
下一组 get_action 必须等这次 update_obs 的正常 RPC 确认。
不能在旧 per_step 模式单方面省略 ACK。

反馈仍在 `update_obs` 的 `obs` 中，新增 `astra_meta.chunk_receipt`：

```json
{
  "base_step": 8, "last_step": 10, "proposed_steps": 5,
  "executed_steps": 2,
  "last_executed_action": {
    "left_ee_pose": [0,0,1,1,0,0,0], "left_ee_joint_state": [0.4],
    "right_ee_pose": [0,0,1,1,0,0,0], "right_ee_joint_state": [0.5]
  },
  "tracking_feedback": {"left": {"position_error_m": 0.001}},
  "rejection_index": 2, "discarded_commands": 3,
  "reason": "ik_rejected", "terminated": false
}
```

该示例是字段说明，不是实测动作。索引从 0 开始；丢弃数包括被拒绝的目标。
`executed_steps` 只数接受且执行的正式动作，不包含预热或拒绝。
reason 可为 `chunk_complete`、`ik_rejected`、`tracking_interrupted`、`episode_end`，
或非法 payload 的错误代码。全部拒绝时 executed_steps=0、last_step=base_step，
last_executed_action=null，仍有新观测与明确回执；客户端必须支持同 step 更新。

IK 拒绝的关键错误代码/数值仍在 `astra_meta.controller_event`，详细文本与逐步日志留服务器。
chunk_boundary 不再另发 `notify_action_rejection`；客户端应在 update_obs 处理回执和事件。
per_step 保留原有拒绝 RPC。客户端不能从 step 相同推断消息是重复包，重复请求仍依据 request_id。
初始观测、最终正常观测、finalize 和 evaluator 流程保留；取消仍走现有清理路径。

## 4. 服务端 JPEG

只对发往 policy 的 `vision.<camera>.color` 使用共享
`XPolicyLab.utils.process_data.encode_image_bit(..., quality=90)`，得到 RGB 标记 JPEG bytes。
客户端通过共享 `decode_image_bit` / `decode_obs_images` 恢复 RGB。
XPolicyLab `PolicyServer` 已在 update_obs 调用前自动解码；手工处理帧的客户端需显式调用解码器。
不能按 BGR 处理解码结果，不能将 JPEG bytes 视为 numpy RGB 数组。

本地 RGB、相机尺寸/路数、depth、state 和标定保持原有语义。
JPEG 有损，不用像素逐位相等验收。诊断分别记录 JPEG 编码耗时/原像素和编码字节，
以及实际 MessagePack 完整消息大小；RPC 往返不是单向网络延迟。

## 5. 统计与验证

应用消息体与 HTTP 请求次数可用于对照，不包含 TLS/TCP 头部、握手、重传或网络 ACK。
不推测公司硬额度，不能以这里的数据保证公司监控口径的流量。
本仓库没有另一机器上的 `Agent/evaluate_tasks.py`，其 lean 轮询设置和真实发送路径需由该机器接入。

离线验证（无需联网/Isaac/模型）：

```bash
/home/lfy/.conda/envs/roboicl-policy/bin/python -m unittest discover -s tests -p 'test_lean_transport.py' -v
/home/lfy/.conda/envs/roboicl-policy/bin/python -m unittest discover -s tests -p 'test_ik_execution.py' -v
```

真实无 LLM 小样本入口为 `setup/validate_lean_transport.py`：使用本机部署的 API 队列、
真实 Isaac Sim、正常生产 rollout、实时双臂保持目标与原控制保护，五步后主动取消并确认清理。
控制和 policy stub 只连 loopback，未测 Cloudflare/公网路径，不产生 LLM 调用。
该 smoke 不能代表任务成功率、公网带宽或公司监控的整局上行量。
结果保存在 `results/lean-transport-smoke.json`；最终实测结果另见下文记录。

完整 HTTP 契约：[remote-simulation-openapi.yaml](remote-simulation-openapi.yaml)。

### 本次真实小样本记录

同一 deposit_coin、seed=0、layout=0、5 步实时双臂保持动作；不调用 LLM。
各次为新仿真进程，调度到不同 GPU，不以启动时间差归因传输选项。

| 配置 | run_id | GPU | update_obs 次数 | get_action 完整回包 bytes | policy stub 发出应用 bytes | 收到应用 bytes |
| --- | --- | --- | --- | --- | --- | --- |
| legacy / per_step / raw_rgb | `8cc60296427c41f8b364c7f6c64e7037` | 0 | 6 | 1,808 | 4,601 | 16,600,790 |
| compact / per_step / raw_rgb | `e8b098a2c0474be4a2325db496c8a1cc` | 2 | 6 | 730 | 3,523 | 16,600,907 |
| compact / chunk_boundary / raw_rgb | `984325e5cd1c4afdae14f8d1cc0b95e4` | 3 | 2 | 730 | 2,263 | 5,535,665 |
| compact / chunk_boundary / jpeg90 | `2154b6f1178d48b386328975765d3b65` | 1 | 2 | 730 | 2,263 | 331,866 |

compact 是 ee_targets_f32_v1 的表格简称。每次都确认 completed_steps=5、
status=cancelled、cleanup_complete=true；首帧 52.872–58.313 秒。
动作回包单独减少 59.6%；分组反馈单独减少 4 次 update_obs，仍本地逐步采集。
chunk raw_rgb 的单条观测包约 2,766,967 bytes，jpeg 为 165,037 bytes，减少约 94.0%。
完整精简配置相对旧协议本次收到应用 bytes 减少约 98.0%，发出应用 bytes 减少约 50.8%。
这里的外发包括本 fixture 的动作回包、观测 ACK、握手/其他 RPC 回包，不包括 HTTP、
WebSocket/TLS/TCP 头部和网络 ACK；stub 回执较小，不可直接代替真实客户端整局数据。

原始记录：`results/lean-transport-smoke.json` 与 `results/lean-transport-isolated-smoke.json`。
可查询 `/v1/runs/<run_id>/diagnostics` 和 `/v1/runs/<run_id>/logs?source=client&tail_lines=200`。
真实样本验证有效三路 640×480 RGB、正常生产 rollout、鉴权 gateway 配对和取消回收；
异常分支通过离线 21 项检查覆盖，未新增公网联调或公司监控口径抓包。

重新测量全部四项（会启动真实仿真，结束主动取消）：

```bash
/home/lfy/.conda/envs/roboicl-policy/bin/python setup/validate_lean_transport.py
```
