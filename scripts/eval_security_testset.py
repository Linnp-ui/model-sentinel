"""安全测试集评测:L1(inspect_text 本地) + L2(laya-l2.service),场景x问法矩阵

用法: .venv/bin/python scripts/eval_security_testset.py [jsonl路径]
口径(镜像 main._review_text 网关门，2026-09-23 起取消字数触发):
  L2 仅当 risk in [20,60) 灰区时调用;conf 应被拦(risk>=60 或 gated-L2 判密);
  pass 应放行(risk<60 且 gated-L2 未判密)。未进灰区记 noL2(纯 L1 判定)。
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from gateway.inspection import inspect_text  # noqa: E402

DATA = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    ROOT, "tests", "fixtures", "security_testset.jsonl")
L2_URL = os.environ.get("AI_GATEWAY_L2_SHADOW_URL", "http://127.0.0.1:8003/classify")
L2_TH = 0.7


def l2_classify(text):
    try:
        req = urllib.request.Request(
            L2_URL, data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        probs = d.get("probabilities", {})
        p = probs.get("CONFIDENTIAL", 0.0)
        return p >= L2_TH, p
    except Exception as e:
        return None, "ERR:%s" % e


def main():
    rows = [json.loads(l) for l in open(DATA, encoding="utf-8")]
    leaks, fps = [], []
    scn = {}  # scenario -> [conf_caught, conf_n, pass_clean, pass_n]
    phr = {}
    for r in rows:
        risk = inspect_text(r["text"]).get("risk_score", 0)
        gated = 20 <= risk < 60  # 网关 L2 门：只看灰区，不计字数
        if gated:
            l2hit, p = l2_classify(r["text"])
            l2hit = bool(l2hit)
        else:
            l2hit, p = False, "noL2"
        if r["expect"] == "confidential":
            caught = risk >= 60 or l2hit
            layer = "L1-block" if risk >= 150 else ("L1-local" if risk >= 60 else
                                                    ("L2" if l2hit else "LEAK"))
            k = scn.setdefault(r["scenario"], [0, 0, 0, 0])
            k[0] += caught
            k[1] += 1
            k2 = phr.setdefault(r["phrasing"], [0, 0, 0, 0])
            k2[0] += caught
            k2[1] += 1
            if not caught:
                leaks.append((r["id"], risk, p, layer, r["text"][:70]))
        else:
            clean = risk < 60 and not l2hit
            layer = "FP-block" if risk >= 150 else ("FP-local" if (risk >= 60 or l2hit) else "ok")
            k = scn.setdefault(r["scenario"], [0, 0, 0, 0])
            k[2] += clean
            k[3] += 1
            k2 = phr.setdefault(r["phrasing"], [0, 0, 0, 0])
            k2[2] += clean
            k2[3] += 1
            if not clean:
                fps.append((r["id"], risk, p, layer, r["text"][:70]))
    tc = sum(v[0] for v in scn.values())
    tn_c = sum(v[1] for v in scn.values())
    tp = sum(v[2] for v in scn.values())
    tn_p = sum(v[3] for v in scn.values())
    print("conf 拦截 %d/%d (%.0f%%)  pass 放行 %d/%d (%.0f%%)" % (
        tc, tn_c, 100 * tc / tn_c, tp, tn_p, 100 * tp / tn_p))
    print("\n[场景] conf拦截率 / pass放行率")
    for s, v in sorted(scn.items()):
        print("  %-9s conf %d/%d  pass %d/%d" % (s, v[0], v[1], v[2], v[3]))
    print("\n[问法] conf拦截率 / pass放行率")
    for s, v in sorted(phr.items()):
        print("  %-9s conf %d/%d  pass %d/%d" % (s, v[0], v[1], v[2], v[3]))
    print("\n[漏网 %d]" % len(leaks))
    for i, risk, p, layer, t in leaks:
        print("  %s risk=%s l2p=%s %s" % (i, risk, p, t))
    print("\n[误伤 %d]" % len(fps))
    for i, risk, p, layer, t in fps:
        print("  %s %s risk=%s l2p=%s %s" % (i, layer, risk, p, t))


if __name__ == "__main__":
    main()
