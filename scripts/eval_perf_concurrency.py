"""并发吞吐 / P99 / 长文本(>100KB) 性能压测（补 7.3 性能覆盖不足，供第 6 章 6.4.1 节）

方法：
- 进程内 ASGI 压测（httpx ASGITransport），与生产单进程 asyncio 部署同构；
- 隔离口径同 tests/conftest.py：内存后端（不落生产审计库）、OLLAMA_MOCK=true、
  临时 routing.yaml 将 on_missing_key 置为 mock ⇒ allow/route_local 的最终上游
  全部由 mock 承接，**零真实出境、零外部凭据**；
- L2 语义判定走真实网络调用（生产 L2 qwen :8002，SMALL_MODEL_TIMEOUT=5s 同生产）；
- 载荷取自 pilot 集（184 条，与 6.3 同一分布），每条追加唯一后缀绕过 L2 判定缓存
  （生产流量文本天然唯一，缓存命中会高估吞吐），后缀不改变 L1 风险分（启动时断言）；
- 每档并发 3 次重复，报告 3 次中位数（与 6.4 口径一致）。

用法: .venv/bin/python scripts/eval_perf_concurrency.py
输出: docs/thesis/perf-concurrency.json
"""
import asyncio
import json
import os
import random
import statistics
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- 隔离 env（必须在 import app 之前；口径同 tests/conftest.py） ----
TMP = tempfile.mkdtemp(prefix="gw-perf-")
PERF_KEY = "sk_perf_eval_" + os.urandom(8).hex()
os.environ["AI_GATEWAY_DEV_API_KEY"] = PERF_KEY
os.environ["OLLAMA_MOCK"] = "true"
os.environ["SMALL_MODEL_ENABLED"] = "true"
os.environ["AI_GATEWAY_L2_BACKEND"] = "qwen"
os.environ["SMALL_MODEL_URL"] = "http://127.0.0.1:8002/v1/chat/completions"
os.environ["SMALL_MODEL_NAME"] = "Qwen3-4B-Instruct-2507"
os.environ["SMALL_MODEL_TIMEOUT"] = "5"
# 明细/审计/会话全部内存后端，绝不写生产库
for _v in ("ADMIN_DB_URL", "DATABASE_URL", "AUDIT_MYSQL_URL", "AUDIT_DATABASE_URL", "REDIS_URL"):
    os.environ[_v] = ""
# 压测不设限流（生产 120 RPM / 10 RPS 会把压测打成 429）
os.environ["AI_GATEWAY_RATE_LIMIT_RPM"] = "0"
os.environ["AI_GATEWAY_RATE_LIMIT_RPS"] = "0"
# L2 持久化覆盖 / 规则首现落盘 指向临时文件，不碰生产 var/
_ov = os.path.join(TMP, "l2_overrides_empty.yaml")
open(_ov, "w").close()
os.environ["AI_GATEWAY_L2_OVERRIDES_PATH"] = _ov
os.environ["AI_GATEWAY_SUGGEST_RULE_SEEN_PATH"] = os.path.join(TMP, "seen.json")
# 临时 routing.yaml：on_missing_key=mock ⇒ 无上游凭据时 allow 走 mock，零出境
_rtmp = os.path.join(TMP, "routing.yaml")
_rtmp_src = os.path.join(ROOT, "routing.yaml")
_rtmp_txt = open(_rtmp_src, encoding="utf-8").read().replace("on_missing_key: reject", "on_missing_key: mock")
assert _rtmp_txt != open(_rtmp_src, encoding="utf-8").read(), "routing.yaml 替换失败"
open(_rtmp, "w", encoding="utf-8").write(_rtmp_txt)
os.environ["AI_GATEWAY_ROUTING_PATH"] = _rtmp

sys.path.insert(0, ROOT)
from src.gateway.main import app  # noqa: E402
from src.gateway import small_model  # noqa: E402
from src.gateway.inspection import inspect_text  # noqa: E402
from src.gateway.admin_store import get_admin_store  # noqa: E402

DATA = os.path.join(ROOT, "tests", "fixtures", "pilot_dataset.jsonl")
OUT = os.path.join(ROOT, "docs", "thesis", "perf-concurrency.json")
LEVELS = [1, 4, 8, 16, 32, 64]
N_PER_LEVEL = 300
RUNS = 3
SEED = 20261102
MODEL = "deepseek/deepseek-flash"   # 外网候选（mock 承接）；非别名组，不触发复杂度选头
SUFFIX = "（压测序号 {i}）"          # 唯一化文本、绕过 L2 缓存；断言不改变风险分

ROWS = [json.loads(l) for l in open(DATA, encoding="utf-8") if l.strip()]
rng = random.Random(SEED)


def _pct(sorted_lat: list[float], p: float) -> float:
    return sorted_lat[min(len(sorted_lat) - 1, int(len(sorted_lat) * p))]


def _median_of(vals: list) -> float:
    return statistics.median(vals)


async def _post(client, text: str, session: str):
    t0 = time.perf_counter()
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {PERF_KEY}", "x-session-id": session},
        json={"model": MODEL, "messages": [{"role": "user", "content": text}], "stream": False},
    )
    lat_ms = (time.perf_counter() - t0) * 1000
    body = {}
    try:
        body = r.json()
    except Exception:
        pass
    return lat_ms, r.status_code, (body.get("gateway") or {}).get("action")


async def _run_level(client, conc: int, n: int, run: int) -> dict:
    payloads = [rng.choice(ROWS) for _ in range(n)]
    texts = [p["text"] + SUFFIX.format(i=run * 100000 + k) for k, p in enumerate(payloads)]
    sessions = [f"perf-{conc}-{run}-{k}" for k in range(n)]
    sem = asyncio.Semaphore(conc)
    lats, bad = [], []

    async def one(k: int):
        async with sem:
            lat, status, action = await _post(client, texts[k], sessions[k])
            if status != 200:
                bad.append((status, action, texts[k][:40]))
            lats.append(lat)

    t0 = time.perf_counter()
    await asyncio.gather(*(one(k) for k in range(n)))
    wall = time.perf_counter() - t0
    s = sorted(lats)
    return {
        "qps": round(n / wall, 1),
        "p50_ms": round(_pct(s, 0.50), 2),
        "p95_ms": round(_pct(s, 0.95), 2),
        "p99_ms": round(_pct(s, 0.99), 2),
        "errors": bad,
    }


async def _run_longtext(client) -> list:
    filler = ("这是一段用于长文本性能测试的中性填充文本，仅用于验证系统在超长输入下的行为，"
              "不涉及任何业务内容。\n")
    assert inspect_text(filler * 200).get("risk_score", 0) == 0, "填充文本本身带风险分，需换文案"
    tail_conf = "附：员工张三 身份证号 110101199001011237 本月工资 15000.50 元。"
    tail_benign = "以上为长文本测试结束标记，全文为中性内容。"

    def build(size: int, variant: str) -> str:
        tail = tail_conf if variant == "confidential" else tail_benign
        base = size - len(tail)
        text = (filler * (base // len(filler) + 1))[:base] + tail
        assert len(text) == size
        return text

    sizes = [(100_000, "confidential"), (300_000, "confidential"), (300_000, "benign"),
             (500_000, "confidential"), (1_048_576, "confidential"), (1_048_576, "benign")]
    out = []
    for size, variant in sizes:
        text = build(size, variant)
        small_model._CACHE.clear()  # 5 次重复不得命中判定缓存
        lats, actions, errs = [], [], []
        for k in range(5):
            lat, status, action = await _post(client, text, f"longtext-{size}-{variant}-{k}")
            lats.append(lat)
            actions.append(action)
            if status != 200:
                errs.append(status)
        s = sorted(lats)
        out.append({
            "size_chars": size, "variant": variant,
            "actions": sorted(set(actions)),
            "median_ms": round(statistics.median(lats), 1),
            "p99_ms": round(_pct(s, 0.99), 1),
            "errors": errs,
        })
    # 1MiB × 并发 8：验证线程池化后事件循环不被长文本审查堵死
    text = build(1_048_576, "confidential")
    small_model._CACHE.clear()
    sem = asyncio.Semaphore(8)
    lats = []

    async def one8(k):
        async with sem:
            lat, status, _ = await _post(client, text, f"longtext-conc8-{k}")
            lats.append(lat)
            assert status == 200

    await asyncio.gather(*(one8(k) for k in range(24)))
    s = sorted(lats)
    out.append({"size_chars": 1_048_576, "variant": "confidential", "concurrency": 8,
                "n": 24, "p99_ms": round(_pct(s, 0.99), 1)})
    return out


async def _smoke(client) -> None:
    cases = [
        ("请解释一下 TCP 三次握手的原理。", "allow"),
        ("员工张三 身份证 110101199001011237 这个月工资 12000 元", "route_local"),
    ]
    gray = next(r for r in ROWS if 20 <= inspect_text(r["text"]).get("risk_score", 0) < 60)
    cases.append((gray["text"] + "（冒烟灰区样本）", None))  # 灰区：L2 判定，动作不定
    for i, (text, expect) in enumerate(cases):
        lat, status, action = await _post(client, text, f"smoke-{i}")
        assert status == 200, (status, action, text[:30])
        if expect:
            assert action == expect, (action, expect, text[:30])
        print(f"smoke {i}: status={status} action={action} lat={lat:.1f}ms  {text[:24]!r}")


async def _main() -> None:
    loop = asyncio.get_running_loop()
    for h in list(app.router.on_startup):
        await h()
    store = get_admin_store()
    try:
        _, found, _ = store.find_key_entry(PERF_KEY)
    except Exception:
        found = False
    if not found:
        store.create_api_key(PERF_KEY, "perf-eval", "", "并发压测（内存后端，不落盘）")

    # 后缀不改变 L1 风险分（184 条全量断言）
    for r in ROWS:
        a = inspect_text(r["text"]).get("risk_score", 0)
        b = inspect_text(r["text"] + SUFFIX.format(i=1)).get("risk_score", 0)
        assert a == b, (r["id"], a, b)

    # 载荷分布：灰区（20<=risk<60，L1 放行后送 L2）占比
    risks = [inspect_text(r["text"]).get("risk_score", 0) for r in ROWS]
    l2_rate = sum(1 for x in risks if 20 <= x < 60) / len(risks)
    print(f"pilot 分布: risk>=60 {sum(1 for x in risks if x >= 60)}/{len(risks)}, "
          f"灰区(20-60) {sum(1 for x in risks if 20 <= x < 60)}/{len(risks)} "
          f"({l2_rate:.1%}，实际 L2 调用率以实测为准), risk<20 {sum(1 for x in risks if x < 20)}/{len(risks)}")

    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://perf") as client:
        await _smoke(client)
        conc = {}
        for c in LEVELS:
            per = []
            for run in range(RUNS):
                res = await _run_level(client, c, N_PER_LEVEL, run)
                small_model._CACHE.clear()
                assert not res["errors"], res["errors"][:3]
                per.append(res)
                print(f"conc={c:2d} run={run}: qps={res['qps']:7.1f} "
                      f"p50={res['p50_ms']:8.1f}ms p95={res['p95_ms']:8.1f}ms p99={res['p99_ms']:8.1f}ms")
            conc[str(c)] = {
                "runs": per,
                "qps": round(_median_of([p["qps"] for p in per]), 1),
                "p50_ms": round(_median_of([p["p50_ms"] for p in per]), 2),
                "p95_ms": round(_median_of([p["p95_ms"] for p in per]), 2),
                "p99_ms": round(_median_of([p["p99_ms"] for p in per]), 2),
            }
        longtext = await _run_longtext(client)
        for row in longtext:
            print("longtext:", json.dumps(row, ensure_ascii=False))

    res = {
        "method": "进程内 ASGI 压测（httpx ASGITransport），单进程 asyncio，与生产部署同构；"
                  "上游 mock 承接（零出境），L2 真调生产 qwen :8002",
        "l2_backend": "qwen Qwen3-4B-Instruct-2507 (:8002, timeout 5s)",
        "n_per_level": N_PER_LEVEL, "runs": RUNS, "seed": SEED,
        "payload": "pilot 集 184 条（与 6.3 同分布），逐条加唯一后缀绕 L2 判定缓存",
        "pilot_risk_dist": {
            "l1_block(>=60)": sum(1 for x in risks if x >= 60),
            "gray(20-60)": sum(1 for x in risks if 20 <= x < 60),
            "low(<20)": sum(1 for x in risks if x < 20),
            "total": len(risks),
        },
        "concurrency": conc,
        "longtext": longtext,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n写入 {OUT}")


if __name__ == "__main__":
    asyncio.run(_main())
