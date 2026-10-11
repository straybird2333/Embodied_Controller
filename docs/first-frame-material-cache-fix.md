# 首帧长等待的定位与修复

日期：2026-10-10（Asia/Shanghai）。部署代码基于
`df7b195dca7b71756df7acc95b7dfc0f131803b6`，修改位于当前工作目录，尚未提交新的 commit。
代码指纹：`results/material-cache-code-fingerprint.json`。

## 原因与证据

此前两次首帧等待分别为 906.21、1072.12 秒，场景重载分别占 852.39、1018.65 秒。
新增实时计时和每 60 秒一次的调用栈采样，将卡点定位到：

```text
SceneManager.reload_envs → IsaacRLEnv.sim_step
→ SimulationContext.step(render=False) → PhysicsContext._step → PhysX simulate()
```

未安装本地映射的同期对照 `0c24d2345efa44c88b4b6f333080d52b`（GPU 4）中，
写入仿真状态仅 0.000924 秒，随后主线程重复停在 `simulate()`。
进程持有 AWS S3 HTTPS 连接，出现重传且没有正文接收进展。
该对照被主动取消，没有完整首帧结果。

同一 `Plastic_ABS.mdl` 通过服务器现有代理 0.991 秒取得 1522 字节，直连超过
10 秒后超时。此前日志报告三个远程 MDL 模块加载失败。将三个原始模块映射到
本地后，首次重载步降至约 0.12 秒，首帧恢复到一分钟左右。
这些证据支持分钟级延迟来自远程材质解析/资源等待；尚未取得原生 C++ 线程栈，
无法指出 PhysX 内部具体的锁或资源解析函数。

## 已部署的修复

- `setup/prepare_material_cache.py` 一次性下载三个原版 NVIDIA MDL 及六个实际引用的 PNG，保存 SHA-256。
- `roboicl/asset_cache.py` 在创建环境前安装精确的 URL 到本地文件映射，并验证通过原 URL 读取到的字节。
- 缓存位于 `data/remote-material-cache/`：9 文件、15,892,492 字节。运行期间只读已有缓存。
- 冻结资产与锁定子模块保持干净；新 run manifest 记录缓存指纹，诊断事件记录实际使用的缓存。
- 首次仿真步内部使用 begin/end 计时，调用卡住时也能查询。计时在第一次 reset 创建内部仿真器后安装。
- API runner 的子进程清理等待缩短到 supervisor 的 20 秒窗口内，避免 runner 被提前杀死后遗留仿真进程。

材质与纹理使用下载到的原始内容。动作格式、物理步长、相机配置、控制保护、
稳定性检查和准备循环沿用原路径。

另一部署目录在仓库根目录准备缓存：

```bash
python setup/prepare_material_cache.py
# 必要时显式指定准备阶段使用的 HTTP 代理：
python setup/prepare_material_cache.py --proxy http://127.0.0.1:7890
```

脚本默认沿用当前代理环境。运行任务读取本地缓存，不依赖这条下载链路。
没有缓存 manifest 时沿用原解析路径；已登记文件缺失或损坏时明确失败。

## 真实 API 测量

以下均通过现有队列启动新仿真进程，没有 LLM 调用。首帧从 API 接收任务计到
服务器三路有效观测可用，不包含公网接收或 LLM 时间。

| run_id | GPU | layout / 模式 | 首帧 | 首次重载步 | 终态 / 清理 |
| --- | --- | --- | --- | --- | --- |
| `b961fd4ed4794ea6b89e34dc951c489b` | 5 | 0 / first_observation | 54.21 s | 0.124 s | succeeded / complete |
| `df64023ca25041da8b6af6cfbc7dad87` | 6 | 1 / first_observation | 61.57 s | 0.125 s | succeeded / complete |
| `28627d2127104470bcf66fec1fa2f177` | 7 | 0 / fixed_steps=5 | 73.34 s | 0.129 s | succeeded / complete |

第一轮验证三条 MDL URL 映射；第二、三轮使用完整九文件缓存，并同时启动，
存在初始化和渲染竞争。不同 GPU/启动条件的秒级差异不能全部归因于缓存。
三轮均取得有效的三路 640×480 uint8 RGB。完整缓存两轮日志均未出现原来的
MDL 模块缺失/无效错误。

固定步数诊断实际接受 5 个保持姿态动作，拒绝数 0。首帧采集 0.383644 秒，
随后五次实际采集为：

```text
0.104540, 0.104638, 0.104375, 0.105130, 0.104316 秒
```

动作调用为 0.214243、0.000406、0.000409、0.000358、0.000356 秒。
保持姿态可能触发已有控制路径的立即返回，不能代表复杂运动成本。
诊断结果为 `diagnostic_success=true`、`task_success=null`。
旧诊断的内外观测 span 同名，以上数字只取实际 get_obs 的 span；新代码将外层
改名为 `observation_capture_cycle`，避免同名摘要重复累计。

取消验证 `2f11d8787c9d484ca6d08565b3d9a314` 在应用启动期间取消，
1.018 秒后进入 cancelled，cleanup_complete=true，仿真 PID 已不存在。
旧 runner 的两个取消对照遗留进程已定向回收。最后检查 API ready=true，8 卡槽位均可用。

查询完整记录沿用已有 API 与 key：

```bash
RUN_ID=df64023ca25041da8b6af6cfbc7dad87
curl -fsS -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  "$API/v1/runs/$RUN_ID/diagnostics?after_seq=0&limit=1000"
curl -fsS -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  "$API/v1/runs/$RUN_ID/logs?source=client&tail_lines=200"
```

服务器查询快照位于 `results/api_<run_id>/diagnostics-summary.json`。

## 剩余耗时与验证范围

当前首帧包含应用启动约 14–15 秒、环境创建约 20–33 秒、reset 约 18–25 秒。
稳定性检查约 1.8 秒，预热约 13–20 秒。后续常驻环境复用可以针对这些启动开销。
本次验证了 deposit_coin 两个布局；其他任务若有额外远程依赖，需要扩展缓存。
公网 WSS、JPEG 收益和真实 LLM 闭环耗时由外部客户端另行测量。

技术依据：[NVIDIA URL aliases](https://docs.omniverse.nvidia.com/kit/docs/client_library/latest/docs/python.html#omni.client.set_alias)、
[HTTP 超时与重试](https://docs.omniverse.nvidia.com/kit/docs/client_library/latest/index.html#http)。
