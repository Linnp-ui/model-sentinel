"""模拟真实流量-回放：把生成集逐条经网关全链路打一遍（顺序，<=4 RPS，不触发限流）。

模型固定 vllm_local/Qwen3.8-AB（内容不出境），max_tokens=8 只测链路不烧 KV。
统计 e2e 延迟分位 + 状态码，并打印回放前后 /admin/metrics 关键行 diff。
用法: .venv/bin/python scripts/sim_traffic_replay.py <traffic.jsonl> [--limit 0] [--gap 0.3]
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
import urllib.error

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
sys.path.insert(0, ROOT)

KEY = os.environ["AI_GATEWAY_DEV_API_KEY"]
URL = "http://127.0.0.1:8080/v1/chat/completions"
METRICS = "http://127.0.0.1:8080/admin/metrics"
MODEL = "vllm_local/Qwen3.8-AB"


def metrics_snapshot():
    with urllib.request.urlopen(METRICS, timeout=10) as resp:
        return resp.read().decode()


def keep(lines):
    return [l for l in lines.splitlines()
            if l.startswith(("gateway_l2_", "gateway_internal_ms", "gateway_upstream_ms",
                             "gateway_requests_v2_total", "gateway_stream_errors_total"))
            and not l.startswith("#")]


def send(text, max_tokens):
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens, "stream": False,
                       "messages": [{"role": "user", "content": text}]}).encode()
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {KEY}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            json.load(resp)
            return resp.status, int((time.time() - t0) * 1000), ""
    except urllib.error.HTTPError as e:
        return e.code, int((time.time() - t0) * 1000), f"http_{e.code}"
    except Exception as e:
        return 0, int((time.time() - t0) * 1000), repr(e)[:80]


def pct(vals, p):
    if not vals:
        return 0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p / 100))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--gap", type=float, default=0.3)
    ap.add_argument("--max-tokens", type=int, default=8)
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.dataset, encoding="utf-8") if l.strip()]
    if args.limit:
        rows = rows[:args.limit]
    before = {l for l in keep(metrics_snapshot())}

    lat, codes, errs = [], {}, []
    t_all = time.time()
    for i, r in enumerate(rows):
        st, ms, err = send(r["text"], args.max_tokens)
        lat.append(ms)
        codes[st] = codes.get(st, 0) + 1
        if err:
            errs.append((i, r["class"], err))
        if (i + 1) % 20 == 0:
            print(f"...{i + 1}/{len(rows)} p50={pct(lat, 50)}ms", flush=True)
        time.sleep(args.gap)
    dt = time.time() - t_all

    print(f"n={len(rows)} 总耗时={dt:.0f}s 平均={dt / max(1, len(rows)):.2f}s/条")
    print(f"e2e_ms p50={pct(lat, 50)} p95={pct(lat, 95)} p99={pct(lat, 99)} max={max(lat) if lat else 0}")
    print(f"status={codes}")
    for i, c, e in errs[:10]:
        print(f"  err #{i} [{c}] {e}")

    after = {l for l in keep(metrics_snapshot())}
    print("--- metrics diff（回放新增） ---")
    for l in sorted(after - before):
        print(f"  + {l}")
    if after == before:
        print("  （无新增计数行：流量可能全命中旧桶，属正常）")


if __name__ == "__main__":
    main()
