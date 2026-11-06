"""重算 stats.json 的训测重叠字段（流程守卫，2026-11-06 增补四十六）

背景：check_train_pilot_overlap 只在 build_laya_dataset.py 全量重建时运行。
real_eval.jsonl 被人工恢复/改标签后（11-05 覆盖事故恢复即如此），stats.json 的
pilot_overlap 字段停留在旧值（曾按 33 条覆盖前集算得 real_eval 同句 2 条，实为 4 条，
读字段者被误导）。本脚本对当前 train.jsonl + real_eval.jsonl 复算同一规则并落盘。

用法: python scripts/resync_stats_overlap.py
  - 规则同 build_laya_dataset.check_train_pilot_overlap（双向子串、最短 8 字）
  - 就地更新 stats.json 的 pilot_overlap 字段（先备份 stats.json.bak_overlap_<ts>）
  - 自检：写后回读一致
"""
import json
import os
import shutil
import time

from build_laya_dataset import OUT_DIR, check_train_pilot_overlap


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def main():
    train = load_jsonl(os.path.join(OUT_DIR, "train.jsonl"))
    real_eval = load_jsonl(os.path.join(OUT_DIR, "real_eval.jsonl"))
    overlap = check_train_pilot_overlap(train, real_eval)
    stats_path = os.path.join(OUT_DIR, "stats.json")
    with open(stats_path, encoding="utf-8") as f:
        stats = json.load(f)
    old = stats.get("pilot_overlap") or {}
    bak = stats_path + time.strftime(".bak_overlap_%Y%m%d_%H%M%S")
    shutil.copy2(stats_path, bak)  # stats 含人工恢复字段（real_eval_reviewed），备份防误伤
    stats["pilot_overlap"] = overlap
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    with open(stats_path, encoding="utf-8") as f:  # 自检：写后回读一致
        assert json.load(f)["pilot_overlap"] == overlap, "写后回读不一致"
    print(f"[resync_stats_overlap] pilot 同句 {len(overlap['pilot_in_train'])} 条、"
          f"real_eval 同句 {len(overlap['real_eval_in_train'])} 条"
          f"（更新前 {len(old.get('pilot_in_train', []))}/{len(old.get('real_eval_in_train', []))}）"
          f"→ {stats_path}（备份 {os.path.basename(bak)}）")
    for t in overlap["real_eval_in_train"]:
        print("   real_eval 同句:", t)
    for i in overlap["pilot_in_train"]:
        print("   pilot 同句:", i)


if __name__ == "__main__":
    main()
