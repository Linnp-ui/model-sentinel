#!/usr/bin/env python3
"""history A/B 实测：多轮序列走生产网关全链路（L1 + 常开 L2 + history + 影子）。

对每轮请求：
  A 臂 = 主判决（带 history，现行生产配置）
  B 臂 = l2.history_shadow（同请求无 history 的判定，影子 env 开时留痕）
单轮（第一轮）两臂相同，只记一次。全程打内网模型，不出境、不耗外部额度。

前置：AI_GATEWAY_L2_HISTORY_SHADOW=true（生产已开）。
用法：source .env && .venv/bin/python scripts/l2_history_ab_live.py
"""
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.l2_history_ab_replay import CASES  # noqa: E402

BASE = "https://127.0.0.1:8080/v1/chat/completions"
MODEL = "vllm_local/qwen2.5:7b"  # 裸内网模型名：走完整审查、不出境


def chat(client: httpx.Client, key: str, messages: list) -> dict:
    r = client.post(BASE, headers={"Authorization": f"Bearer {key}"},
                    json={"model": MODEL, "messages": messages}, timeout=90)
    r.raise_for_status()
    return r.json().get("gateway", {})


def b_action(gw: dict) -> str:
    """无 history 口径下这条请求的最终动作（影子 label 按同一阈值推导）。"""
    hs = (gw.get("l2") or {}).get("history_shadow") or {}
    if not hs.get("label"):
        return "-"
    if hs.get("degraded"):
        return "degraded"
    if hs["label"] == "CONFIDENTIAL" and float(hs.get("confidence") or 0) >= 0.7:
        return "route_local"
    return "allow"


def main() -> None:
    key = os.environ.get("AI_GATEWAY_DEV_API_KEY", "")
    if not key:
        sys.exit("AI_GATEWAY_DEV_API_KEY 未设置（先 source .env）")
    client = httpx.Client(verify=False, trust_env=False)
    diff_cases = []
    print(f"{'case':<16} {'轮':<3} {'A臂(带history)':<22} {'B臂(无history)':<16}")
    for c in CASES:
        t1, t2 = c["turns"][0], c["turns"][-1]
        gw1 = chat(client, key, [{"role": "user", "content": t1}])
        l21 = gw1.get("l2") or {}
        print(f"{c['id']:<16} 1   "
              f"{gw1.get('action') or '-'} {l21.get('label') or '-'}"
              f"{'/' + str(l21.get('scope')) if l21.get('scope') else ''}{'  (两臂同)' if not l21.get('history_shadow') else ''}"
              f"{'':<16}")
        gw2 = chat(client, key, [
            {"role": "user", "content": t1},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": t2}])
        l22 = gw2.get("l2") or {}
        a = gw2.get("action") or "-"
        b = b_action(gw2)
        mark = "  ← 差异" if a != b and b != "-" else ""
        if mark:
            diff_cases.append(c["id"])
        print(f"{c['id']:<16} 2   {a} {l22.get('label') or '-'}"
              f"{'/' + str(l22.get('scope')) if l22.get('scope') else ''}"
              f"{'':<6} {b} {l22.get('history_shadow', {}).get('label', '-')}{mark}")
    print(f"\n第二轮动作差异（A≠B）{len(diff_cases)} 例：" + ", ".join(diff_cases))


if __name__ == "__main__":
    main()
