"""真实留出集扩充（2026-11-03）：把 11-02 构建时因规则名语言漂移漏采的
审计样本补进 real_eval.jsonl（只扩评估集，不动 train.jsonl）。

背景：build_laya_dataset.py 的 POS/NEG 规则集只写中文名，早期流量的 rule 列
是英文 slug（default_allow / pii_weighted_route_local / financial_local_only /
block_secrets / pii_critical_block），整批漏采且从未进任何训练集。本脚本按同一
标签口径（deny-overrides）把它们补为留出样本。

用法: python scripts/expand_real_eval.py
输出: ~/models/laya-dataset/{real_eval.jsonl（旧文件备份 .bak_expand_<ts>）, stats.json}
数据含公司机密内容，留在本机 models 目录，不进仓库。幂等：重复运行不重复合并。
"""
import hashlib
import json
import os
import re
import shutil
import time
from collections import Counter

import pymysql

OUT_DIR = os.path.expanduser("~/models/laya-dataset")
AUDIT_URL = dict(host="127.0.0.1", user="root", password="infini_rag_flow",
                 database="gateway_audit", charset="utf8mb4")
MIN_CHARS = 30  # 与 build_laya_dataset.MIN_CHARS 一致

# 双语规则映射（与 build_laya_dataset.POS_RULES/NEG_RULES 同口径，补英文 slug）
POS_RULES = {"l2_confidential", "隐私加权转内网", "pii_weighted_route_local",
             "财务表转内网", "financial_local_only", "拦截密钥", "block_secrets",
             "高危隐私拦截", "pii_critical_block", "图纸转内网"}
NEG_RULES = {"默认放行", "default_allow"}
# 歧义规则排除（同 build_laya_dataset 口径）：会话级/可用性兜底/注入/别名旁路等

def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

def main():
    conn = pymysql.connect(**AUDIT_URL)
    cur = conn.cursor()
    cur.execute("SELECT rule, text_preview FROM audit_logs "
                "WHERE text_preview IS NOT NULL AND CHAR_LENGTH(text_preview) >= %s",
                (MIN_CHARS,))
    rows = cur.fetchall()
    conn.close()

    # 审计库全量去重（冲突取密，与 build 脚本一致）
    by_hash = {}
    for rule, text in rows:
        n = norm(text)
        label = 1 if rule in POS_RULES else (0 if rule in NEG_RULES else None)
        if label is None:
            continue
        h = hashlib.sha1(n.encode()).hexdigest()
        if h in by_hash:
            if label == 1 and by_hash[h]["label"] == 0:
                by_hash[h]["label"], by_hash[h]["source"] = 1, f"audit:{rule}"
        else:
            by_hash[h] = {"text": n, "label": label, "source": f"audit:{rule}"}

    train_texts = set()
    with open(os.path.join(OUT_DIR, "train.jsonl"), encoding="utf-8") as f:
        for line in f:
            t = json.loads(line).get("text")
            if t:
                train_texts.add(t)

    real_path = os.path.join(OUT_DIR, "real_eval.jsonl")
    old = [json.loads(l) for l in open(real_path, encoding="utf-8")]
    old_texts = {r["text"] for r in old}

    new = []
    for h in sorted(by_hash):  # sha1 排序 = 确定性
        v = by_hash[h]
        if v["text"] in train_texts or v["text"] in old_texts:
            continue
        # ts 从审计库回填（仅展示用）
        new.append(v)
    # 与审计库 ts 对齐：重新查一次 ts（按文本 sha1 join）
    if new:
        conn = pymysql.connect(**AUDIT_URL)
        cur = conn.cursor()
        cur.execute("SELECT text_preview, ts FROM audit_logs "
                    "WHERE CHAR_LENGTH(text_preview) >= %s", (MIN_CHARS,))
        ts_map = {}
        for text, ts in cur.fetchall():
            n = norm(text)
            h = hashlib.sha1(n.encode()).hexdigest()
            if h not in ts_map:
                ts_map[h] = str(ts)
        conn.close()
        for v in new:
            h = hashlib.sha1(v["text"].encode()).hexdigest()
            v["ts"] = ts_map.get(h, "")
            v["filename"] = ""

    # 幂等：候选与现文件完全一致 = 已扩过
    if not new:
        print("无可补充样本（已扩充过或无新候选）")
        return

    bak = real_path + f".bak_expand_{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(real_path, bak)
    with open(real_path, "w", encoding="utf-8") as f:
        for r in old + new:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    stats_path = os.path.join(OUT_DIR, "stats.json")
    stats = json.load(open(stats_path, encoding="utf-8"))
    all_items = old + new
    stats["real_eval"] = len(all_items)
    stats["real_eval_pos"] = sum(1 for r in all_items if r["label"] == 1)
    stats["real_eval_neg"] = sum(1 for r in all_items if r["label"] == 0)
    stats["real_eval_expanded"] = {
        "date": time.strftime("%Y-%m-%d"),
        "added": len(new),
        "note": "补采 11-02 构建因规则名语言漂移漏采的英文 slug 审计样本"
                "（从未进 train），标签口径同 build_laya_dataset（deny-overrides）",
        "added_source": dict(Counter(r["source"] for r in new)),
    }
    json.dump(stats, open(stats_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"扩充: +{len(new)} → real_eval 共 {len(all_items)} 条"
          f"（密 {stats['real_eval_pos']}/放 {stats['real_eval_neg']}）; 备份 {os.path.basename(bak)}")
    print("新增 source:", dict(Counter(r["source"] for r in new)))

if __name__ == "__main__":
    main()
