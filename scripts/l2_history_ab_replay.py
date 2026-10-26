#!/usr/bin/env python3
"""history 档 A/B 回放：同一组多轮对话，「带 history 块」vs「不带」分别过 L2，对比判定。

用法：
  .venv/bin/python scripts/l2_history_ab_replay.py            # 内置语料
  .venv/bin/python scripts/l2_history_ab_replay.py cases.json # 自定义 [{"id":..,"turns":[..]}]

只打内网 L2（不出境），不改任何线上配置。判定带温度 0.1 采样噪声，单跑一次给方向；
要稳就重跑几次看方向是否一致。
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.gateway import small_model as sm  # noqa: E402

CC = 1200  # 与 model_policy.chunk_chars 一致
HISTORY_LIMIT = 2  # 与 main._prev_user_text_* 的 limit 一致

CASES = [
    # A 组：机密上下文 + 指代性追问 —— 期望 带history=CONFIDENTIAL / 不带=NORMAL（增量拦截）
    {"id": "A1 薪酬表追问", "turns": ["这份薪酬表帮我核对一下", "把第三列改成 24055"]},
    {"id": "A2 营收纠错", "turns": ["上季度营收 4800 万，利润率 12%", "第二行那个数字写错了吧"]},
    {"id": "A3 合同改期", "turns": ["合同金额 480 万，分三期支付", "付款周期改成 60 天"]},
    {"id": "A4 暗语金额", "turns": ["那个数你懂的,38后面加个k", "行,就按这个数走"]},
    {"id": "A5 图纸指代", "turns": ["结构图号 A-12 的施工图", "第三张图放大看看"]},
    # B 组：机密上下文 + 明确转话题 —— 测误报方向（带 history 仍判密 = 过度拦截，安全但体验差）
    {"id": "B1 转天气", "turns": ["薪酬表核对完了", "好的谢谢，顺便查下明天天气"]},
    {"id": "B2 转订会议室", "turns": ["预算表里的数字对一下", "对了，明天下午的会议室帮我订一下"]},
    {"id": "B3 保密后翻译", "turns": ["工资条发你了，注意保密", "帮我翻译 hello world"]},
    # C 组：全程无害 —— 两组都该 NORMAL
    {"id": "C1 CAP→PACELC", "turns": ["CAP 定理是什么", "那 PACELC 呢"]},
    {"id": "C2 快排", "turns": ["写个 python 排序", "换成快速排序"]},
]


def prev_user(turns, limit=HISTORY_LIMIT) -> str:
    return "\n".join(turns[-(limit + 1):-1]) if len(turns) > 1 else ""


async def run_case(c: dict) -> dict:
    last, hist = c["turns"][-1], prev_user(c["turns"])
    t0 = time.monotonic()
    wo = await sm.classify(last[:CC])
    t1 = time.monotonic()
    if hist:
        with_r = await sm.classify_chunks([last[:CC], hist[-CC:]],
                                          scopes=["last_user", "history"])
    else:
        with_r = wo
    t2 = time.monotonic()
    return {
        "id": c["id"],
        "with_label": with_r.get("label"), "with_conf": with_r.get("confidence"),
        "with_ms": int((t2 - t1) * 1000),
        "wo_label": wo.get("label"), "wo_conf": wo.get("confidence"),
        "wo_ms": int((t1 - t0) * 1000),
        "with_reason": (with_r.get("reason") or "")[:24],
        "wo_reason": (wo.get("reason") or "")[:24],
    }


async def main() -> None:
    # 对齐生产口径：l2_overrides.yaml 里的持久化提示词（网关启动时注入 env 才生效，
    # 独立脚本要自己读，否则用的是代码内置默认 prompt，判定会漂移）
    try:
        import yaml
        from pathlib import Path
        ovf = Path(__file__).resolve().parent.parent / "l2_overrides.yaml"
        if ovf.exists():
            ov = yaml.safe_load(ovf.read_text(encoding="utf-8")) or {}
            for k in ("AI_GATEWAY_L2_SYSTEM_PROMPT", "AI_GATEWAY_L2_USER_PROMPT_TEMPLATE"):
                if ov.get(k):
                    os.environ.setdefault(k, ov[k])
    except Exception:
        pass
    cases = json.load(open(sys.argv[1], encoding="utf-8")) if len(sys.argv) > 1 else CASES
    sem = asyncio.Semaphore(4)

    async def one(c):
        async with sem:
            try:
                return await run_case(c)
            except Exception as e:
                return {"id": c["id"], "with_label": "ERR", "wo_label": "ERR",
                        "with_reason": str(e)[:60], "wo_reason": str(e)[:60],
                        "with_conf": 0, "wo_conf": 0, "with_ms": 0, "wo_ms": 0}

    rows = await asyncio.gather(*[one(c) for c in cases])
    print(f"{'case':<16} {'带history':<14} {'不带':<14} {'Δms':>6}  原因(带/不带)")
    agg = {}
    for r in rows:
        key = f"{r['with_label'][:4]}/{r['wo_label'][:4]}"
        agg[key] = agg.get(key, 0) + 1
        dm = r["with_ms"] - r["wo_ms"]
        print(f"{r['id']:<16} {str(r['with_label']) + ' ' + str(r['with_conf']):<14} "
              f"{str(r['wo_label']) + ' ' + str(r['wo_conf']):<14} {dm:>+6}  "
              f"{r['with_reason']} / {r['wo_reason']}")
    print("\n聚合（带history标签/不带标签）:", agg)
    inc = [r for r in rows if r["with_label"] == "CONFIDENTIAL" and r["wo_label"] == "NORMAL"]
    print(f"history 增量拦截 {len(inc)} 例：" + ", ".join(r["id"] for r in inc))
    miss = [r for r in rows if r["with_label"] == "NORMAL" and r["wo_label"] == "CONFIDENTIAL"]
    print(f"history 反而漏（理论上不该有） {len(miss)} 例：" + ", ".join(r["id"] for r in miss))


if __name__ == "__main__":
    asyncio.run(main())
