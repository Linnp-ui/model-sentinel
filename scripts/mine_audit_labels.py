#!/usr/bin/env python3
"""audit 未入集行 → 伪标签半自动挖掘（管理台 B 方案）。

范围：规则推导覆盖不到的行（rule 不在 build_laya_dataset 的 POS/NEG 集合）、
文本 ≥MIN_LABEL_CHARS、其 sha1 没被人标过的（人标最高优先级，跳过）。
判据（自动两条 + 人工兜底）：
  标 1：inspection 客观 PII/凭据 pattern 命中（findings 非空，regex 不撒谎；
        keyword 只是词表不算证据）
  标 0：risk_score < 20（深放行）且影子 p ≤ 0.3（模型也确信非密）
  其余：入 var/label_queue.jsonl 人工队列（管理台标注机制外的暂存，
        标完 SQL/upsert 进 l2_labels 即入下版数据集）
用法：python scripts/mine_audit_labels.py           # dry-run 只打印
      python scripts/mine_audit_labels.py --apply  # 自动标落 l2_labels + 写队列
"""
import hashlib
import importlib.util
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SHADOW = "http://127.0.0.1:8003/classify"
QUEUE = os.path.join(ROOT, "var", "label_queue.jsonl")


def _load_build():
    """复用 build_laya_dataset 的常量与 extract_labeled（已标 sha1 集）。"""
    p = os.path.join(ROOT, "scripts", "build_laya_dataset.py")
    spec = importlib.util.spec_from_file_location("build_laya_dataset", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _shadow_p(text: str):
    try:
        body = json.dumps({"text": text, "filename": ""}).encode()
        req = urllib.request.Request(SHADOW, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["probabilities"]["CONFIDENTIAL"]
    except Exception:
        return None


def main() -> int:
    apply = "--apply" in sys.argv
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
    import pymysql
    from src.gateway.inspection import inspect_text

    b = _load_build()
    MIN_LABEL_CHARS = b.MIN_LABEL_CHARS
    covered = b.POS_RULES | b.NEG_RULES
    existing = set(b.extract_labeled())  # 已人工标过的 sha1

    conn = pymysql.connect(**b.AUDIT_URL)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT req_id, rule, text_preview, ts FROM audit_logs"
            " WHERE LENGTH(text_preview) >= %s AND rule <> ''", (MIN_LABEL_CHARS,))
        rows = cur.fetchall()
    conn.close()

    groups, skipped_labeled = defaultdict(lambda: {"ids": [], "rules": set()}), 0
    scanned = 0
    for req_id, rule, text, ts in rows:
        if rule in covered:
            continue
        scanned += 1
        t = re.sub(r"\s+", " ", text or "").strip()
        if len(t) < MIN_LABEL_CHARS:
            continue
        h = hashlib.sha1(t.encode()).hexdigest()
        if h in existing:
            skipped_labeled += 1
            continue
        g = groups[h]
        g["text"], g["ts"] = t, str(ts)
        g["ids"].append(str(req_id))
        g["rules"].add(str(rule))

    auto = {1: [], 0: []}
    queue = []
    for h, g in groups.items():
        r = inspect_text(g["text"])
        p = _shadow_p(g["text"])
        if r["findings"]:
            g["label"] = 1
            g["why"] = "pattern:" + ",".join(sorted({f["type"] for f in r["findings"]}))
        elif r["risk_score"] < 20 and p is not None and p <= 0.3:
            g["label"] = 0
            g["why"] = f"risk{r['risk_score']}+p{p:.2f}"
        else:
            g["label"] = None
            g["why"] = f"risk={r['risk_score']} kw={r['keyword_score']} p={p}"
        (auto[g["label"]] if g["label"] is not None else queue).append(g)

    print(f"扫描可用行 {scanned}（≥{MIN_LABEL_CHARS} 字，非 POS/NEG 覆盖规则）"
          f" → 去重文本 {len(groups)}（已标跳过 {skipped_labeled}）")
    print(f"自动标 1: {len(auto[1])} | 自动标 0: {len(auto[0])} | 人工队列: {len(queue)}")

    if apply:
        from src.gateway.admin_store import get_admin_store
        st = get_admin_store()
        n = 0
        for lab in (1, 0):
            for g in auto[lab]:
                for rid in g["ids"]:
                    st.upsert_l2_label(rid, lab)
                    n += 1
        with open(QUEUE, "w", encoding="utf-8") as f:
            for g in queue:
                f.write(json.dumps({
                    "text": g["text"], "rules": sorted(g["rules"]),
                    "req_ids": g["ids"], "ts": g["ts"], "why": g["why"],
                }, ensure_ascii=False) + "\n")
        print(f"[apply] 落标注 {n} 行（{len(auto[1])} 文本标 1 / {len(auto[0])} 标 0）；"
              f"队列 {len(queue)} 条 → {QUEUE}")
    else:
        for lab in (1, 0):
            for g in auto[lab][:4]:
                print(f"  自动{lab} [{','.join(sorted(g['rules']))}] {g['why']} | {g['text'][:50]}")
        for g in queue[:8]:
            print(f"  队列  [{','.join(sorted(g['rules']))}] {g['why']} | {g['text'][:50]}")
        print("（dry-run，--apply 落库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
