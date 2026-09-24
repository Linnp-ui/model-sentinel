# 内网 vLLM 主机（IP/端口按实际部署，示例 10.0.0.10）

> 上级：`AGENTS.md`。

| 端口 | systemd unit | 模型 | provider 名 |
|---|---|---|---|
| 8000 | `vllm-<main-model>.service` | `qwen2.5:7b`（本地主力模型） | `vllm_local` |
| 8001 | `vllm-bge-m3.service` | `bge-m3`（embedding） | `bge_m3` |
| 8002 | `vllm-qwen3-4b.service` | `Qwen3-4B-Instruct-2507` (L2 分类) | `vllm_l2` |
| 8003 | `laya-l2.service` | `Laya multilingual`（L2 决策模型，影子双跑） | `AI_GATEWAY_L2_SHADOW_URL` |

> `laya-l2.service` **不是 vllm unit**：Laya 是非自回归决策模型，无 OpenAI 协议、
> vLLM 跑不了，走 Python SDK + FastAPI wrapper（`scripts/laya_l2_server.py`，
> 独立 venv `~/venvs/laya`，torch 经 .pth 复用 `~/venvs/vllm`）。权重
> `~/models/laya/multilingual`（sha256 已验真）；当前跑微调版
> `~/models/laya-finetuned-confidential`（审计语料+合成增广，2026-09-22）；
> 运行时 `HF_HUB_OFFLINE=1` 无外联。换 checkpoint = 换 `LAYA_MODEL_PATH` + 重启 unit。

- 单元 `--port` ↔ `routing.yaml` provider `base_url` ↔ 网关 env **三者一一对应**
- 模板：`scripts/vllm-<model-slug>.service`（安装前替换 `your-user` / 模型路径 / GPU）
- 新增模型前先 `ss -lntp | grep <port>` + `nvidia-smi --query-gpu=index,memory.used --format=csv` 验端口/GPU，再同步本表 + `routing.yaml`

## 硬约束：进程操作必须显式带进程名

多模型共机，**不带进程名的批量 / 通配符会停掉全部模型**，网关侧表现为 L2
分类、embedding、本地降级链路集体 502，排查极难。

```bash
# ✅ 正确
sudo systemctl start   vllm-qwen3-4b.service
sudo systemctl restart vllm-qwen3-4b.service

# ❌ 禁止
pkill -f vllm ; killall vllm ; kill $(pidof vllm)
systemctl restart 'vllm-*'
systemctl stop vllm                              # 同名聚合 unit 一次停全部
```

`install_vllm_*.sh` 里的 `systemctl enable --now` 会**立即启动**；只想登记自启就去掉 `--now`。
`--gpu-memory-utilization` 是**单卡总显存**的占比，多进程共卡要按 `剩余/总` 折算。
