# RoboICL 部署与使用交接

更新时间：2026-10-12

项目目录：`/data1/lfy/Robbot`

最新性能修复：已将导致首次 PhysX 步等待的远程材质依赖缓存到本地，
deposit_coin 两个布局的新进程首帧诊断为 54.21–73.34 秒，后续采集约 0.105 秒。
新 API 任务自动使用缓存。部署、证据及查询命令见
[首帧修复记录](docs/first-frame-material-cache-fix.md)。以下原部署/模型联调信息保留作为历史记录。

## 公网流量精简协议（2026-10-12）

会话已支持显式协商紧凑 float32 动作、chunk_boundary 反馈及服务端 RGB JPEG。
旧客户端默认 legacy/per_step/raw_rgb，保持现有调用方式。
另一机器需检查会话回显再开启新动作格式，并处理分组回执和 JPEG bytes。
具体契约、示例和验证边界见 [docs/lean-policy-transport.md](docs/lean-policy-transport.md)。

## 部署状态

- Ubuntu 主机已重启并使用 NVIDIA 驱动 `580.178.04`；当前内核为 `6.8.0-48-generic`，检测到 8 张 RTX 4090。
- Isaac Sim `5.1.0` 已安装到 `roboicl-sim` Conda 环境。用户已接受 Omniverse EULA。
- RoboICL 子模块已初始化，锁定版本已核对。
- RoboDojo 完整资产位于 `/home/lfy/robodojo-data-cache`（约 66 GB）。`data/Assets` 链接到该缓存，`data/.roboicl-assets.json` 记录了冻结版本 `0a709fe7863f6c869a8d141676585a883fce4270`。
- 已完成 README dry-run 和 Isaac Sim capture-only。三个相机流均正常，图像分辨率为 `640×480`。
- `roboicl-policy`、`roboicl-sim` 两个环境的 `pip check` 均通过。
- 系统已安装 `ffmpeg 4.4.2`；capture-only 成功写出相机探测结果。
- DashScope Qwen `qwen3.8-flash` 的 Responses API 请求成功；RoboICL 真实图像/动作请求也成功驱动仿真至第 40/300 步。为控制额外用量，这次评估被手动中止，没有完整任务得分。

## 环境位置

| 项目 | 路径/值 |
|---|---|
| RoboICL | `/data1/lfy/Robbot` |
| policy Python | `/home/lfy/.conda/envs/roboicl-policy/bin/python` |
| simulator Python | `/home/lfy/.conda/envs/roboicl-sim/bin/python` |
| RoboDojo 资产缓存 | `/home/lfy/robodojo-data-cache` |
| 资产入口 | `/data1/lfy/Robbot/data/Assets` |
| API Key 文件 | `~/.config/dashscope/api_key`（权限 `600`，不要提交或粘贴到聊天） |

## 初始化运行环境

在 Bash 中进入项目目录并设置两个 Python 解释器：

```bash
cd /data1/lfy/Robbot
export ROBOICL_SIM_PYTHON=/home/lfy/.conda/envs/roboicl-sim/bin/python
export ROBOICL_POLICY_PYTHON=/home/lfy/.conda/envs/roboicl-policy/bin/python
export ROBOICL_DATA_ROOT="$PWD/data"
export ROBOICL_RESULTS_ROOT="$PWD/results"
export OMNI_KIT_ACCEPT_EULA=YES
source setup/paths.sh
```

`setup/paths.sh` 会设置项目根目录、数据/结果目录和 `PYTHONPATH`。执行 Isaac Sim 时保留 `OMNI_KIT_ACCEPT_EULA=YES`，避免 Kit 在非交互进程中等待 EULA 输入。

## Qwen 配置

当前 RoboICL 使用 `ASTRA_API_KEY`、profile 中的 `endpoint` 和 `--model`，不会直接读取 `QWEN_BASE_URL`、`QWEN_MODEL` 或 `DASHSCOPE_API_KEY`。DashScope 基址需要补上 `/responses`；当前验证的完整地址是：

```text
https://dashscope.aliyuncs.com/compatible-mode/v1/responses
```

生成保存在用户配置目录的零样本 profile（不修改仓库内的原始 profile）：

```bash
mkdir -p "$HOME/.config/roboicl"
python3 - <<'PY'
import json
from pathlib import Path

source = Path('/data1/lfy/Robbot/configs/protocols/zero_shot_b25.json')
profile = json.loads(source.read_text())
profile['id'] = 'roboicl-0shot-qwen3.8-flash-v1'
profile['model'] = 'qwen3.8-flash'
profile['api_mode'] = 'responses'
profile['endpoint'] = 'https://dashscope.aliyuncs.com/compatible-mode/v1/responses'
target = Path.home() / '.config/roboicl/qwen3.8-flash.json'
target.write_text(json.dumps(profile, indent=2) + '\n')
print(target)
PY
```

DashScope 密钥已保存于 `~/.config/dashscope/api_key`。运行前从私有文件载入，不要把密钥写入 profile、日志或命令参数：

```bash
export ASTRA_API_KEY="$(cat "$HOME/.config/dashscope/api_key")"
```

官方文档：[Qwen3.8-Flash 模型](https://help.aliyun.com/zh/model-studio/qwen3-8-flash)、[OpenAI 兼容 Responses API](https://help.aliyun.com/en/model-studio/qwen-api-via-openai-responses)。

## 预检与运行

### 不调用模型的配置预检

```bash
"$ROBOICL_POLICY_PYTHON" -m roboicl.run deposit_coin \
  --shots 0 --seed 0 --layout 0 \
  --profile "$HOME/.config/roboicl/qwen3.8-flash.json" \
  --model qwen3.8-flash --dry-run
```

### Isaac Sim 与相机预检

不会调用模型，也不会执行机器人动作；需要已接受 EULA 并保留 `OMNI_KIT_ACCEPT_EULA=YES`：

```bash
"$ROBOICL_POLICY_PYTHON" -m roboicl.run deposit_coin \
  --shots 0 --seed 0 --layout 0 \
  --profile "$HOME/.config/roboicl/qwen3.8-flash.json" \
  --model qwen3.8-flash --capture-only
```

### Qwen 零样本仿真评估

下面命令会产生模型调用费用并实际驱动模拟机器人。当前验证使用 `low` 推理强度和 4096 token 输出上限；不要把 `--max-output-tokens` 降至 512，Qwen 会在完成推理前耗尽上限。

```bash
"$ROBOICL_POLICY_PYTHON" -m roboicl.run deposit_coin \
  --shots 0 --seed 0 --layout 0 --gpu 0 \
  --profile "$HOME/.config/roboicl/qwen3.8-flash.json" \
  --model qwen3.8-flash \
  --reasoning-effort low --max-output-tokens 4096
unset ASTRA_API_KEY
```

结束后，每次运行会在 `results/<run-id>/` 下写入 `run.json`、`status.json`、`client.log`、`server.log` 和 `astra/` 请求记录。`status.json` 为 `interrupted` 表示人工中止；只有完整运行及 evaluator 结果才可判断任务分数。按 `Ctrl-C` 可中断当前运行。

## 依赖补充与故障记录

Isaac Sim/直接依赖之外，仿真环境还补装了这些 pinned runtime 依赖：

```bash
conda run -n roboicl-sim python -m pip install \
  'flatdict==4.0.0' 'shapely==2.1.2' 'tqdm==4.67.1' \
  'Rtree==1.3.0' 'yourdfpy==0.0.60'
conda run -n roboicl-sim python -m pip install \
  -e 'third_party/curobo[cu12]' -c setup/constraints-sim.txt
sudo apt-get install -y --no-install-recommends ffmpeg
```

资产下载脚本在本机遇到 RoboDojo 上游 `ckpt` 日志目录中的 7 个非规范空 LFS 指针，导致完整 `git lfs fsck` 返回告警。问题不在 `Assets` 树；资产对象哈希校验通过，冻结版本、manifest、场景和相机预检均通过。资产已经下载并安装完成；除非上游修正这些指针，否则重跑 `setup/fetch_assets.py` 可能再次在 fsck 阶段报错。

## 工作区注意事项

当前目录还有未提交的远程仿真相关改动，包括 `README.md`、`.gitignore`、`roboicl/run.py`、`roboicl/robodojo.py`，以及 `docs/`、`roboicl/remote_policy.py`、`roboicl/service.py` 等新文件。这些不属于本次安装说明的改动，使用前请先检查；不要执行 `git reset --hard` 或 `git clean -fd` 清理它们。
