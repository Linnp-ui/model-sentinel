"""L2=qwen 提示词 A/B：同一 184 例 pilot 上，默认生产提示词 vs 调优后提示词。

用法: .venv/bin/python scripts/eval_qwen_prompt_ab.py
输出: docs/thesis/qwen-prompt-ab.json
说明:
- base = 内置生产 SYSTEM_PROMPT（即 qwen-baseline.json 的口径）；
- tuned = base + 三类增补条款（凭据密钥 / 经营与人事动态 / 英文财务与数字隐写），
  增补条款由 qwen-baseline.json 的 18 条级联漏报错因归纳（论文 §6.5.1）；
- 两组同走 :8002 vLLM、temperature=0、同一 user 模板，单次全量推理；
- cascade 按 C = A_l1 ∨ (risk≥20 ∧ L2判密) ∨ 会话粘性 重算（L1 为确定性规则，
  跨 L2 后端一致）；脚本先以 laya 数据自检，须 184/184 复现 cascade-baseline 的 C。
"""
import asyncio, json, os, sys, datetime
from dotenv import load_dotenv
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT); sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, '.env'))
from src.gateway.small_model import _call_qwen, SYSTEM_PROMPT

TUNED_EXTRA = """- 凭据与密钥：密码、数据库连接串、令牌（含 eyJ 开头的 JWT、ghp_/AKIA/sk- 等格式）、私钥
- 经营与人事动态：高管行程、投融资/并购日程、编制与调薪计划
补充一：英文财务表述（revenue、profit、salary、base、package、offer、compensation 等）伴随具体金额时同样属于机密。
补充二：数字的隐写、拆分或暗语化表述（如"38后面加个k""2加13""那个数你懂的"）指向具体机密金额时，结合上下文可还原具体数值的，判机密。"""

TUNED_PROMPT = SYSTEM_PROMPT.replace("只输出JSON", TUNED_EXTRA + "\n\n只输出JSON")
assert TUNED_PROMPT != SYSTEM_PROMPT, "增补条款未插入成功"

rows = [json.loads(l) for l in open('tests/fixtures/pilot_dataset.jsonl', encoding='utf-8') if l.strip()]
cbase = json.load(open('docs/thesis/cascade-baseline.json'))
cmap = {it['id']: it for it in cbase['items']}

# 会话粘性结构：同 session_id 的前一轮判密 ⇒ 本轮短路（与生产语义一致）
session_judged: dict = {}
for r in rows:
    sid, turn = r.get('session_id'), r.get('turn')
    if sid and turn == 2:
        turn1 = next((x for x in rows if x.get('session_id') == sid and x.get('turn') == 1), None)
        assert turn1 is not None
        session_judged[r['id']] = turn1['id']

def cascade_scores(l2_verdict: dict) -> dict:
    """按 C = A ∨ (risk≥20 ∧ L2) ∨ 会话粘性 重算，返回混淆矩阵。"""
    tp = fn = fp = tn = 0
    for r in rows:
        cv = cmap[r['id']]
        l2 = l2_verdict.get(r['id'])
        stick = session_judged.get(r['id'])
        stick_hit = bool(l2_verdict.get(stick)) if stick else False
        c = bool(cv['A']) or (cv['risk'] >= 20 and bool(l2)) or stick_hit
        conf = r['expect'] == 'confidential'
        if conf and c: tp += 1
        elif conf: fn += 1
        elif c: fp += 1
        else: tn += 1
    return {'recall': round(tp / (tp + fn), 4) if tp + fn else None,
            'fpr': round(fp / (fp + tn), 4) if fp + tn else None,
            'tp': tp, 'fn': fn, 'fp': fp, 'tn': tn}

# 自检：用 laya 的 B 判定复现 cascade-baseline 的 C，须全对
laya_c_ok = 0
for r in rows:
    cv = cmap[r['id']]
    stick = session_judged.get(r['id'])
    stick_hit = bool(cmap[stick]['B']) if stick else False
    c = bool(cv['A']) or (cv['risk'] >= 20 and bool(cv['B'])) or stick_hit
    if c == cv['C']:
        laya_c_ok += 1
assert laya_c_ok == len(rows), f'cascade 公式自检失败: {laya_c_ok}/{len(rows)}'

sem = asyncio.Semaphore(8)
async def one(text, system):
    async with sem:
        try:
            out = await _call_qwen(text, system=system, temperature=0.0)
            return out.get('label') == 'CONFIDENTIAL'
        except Exception:
            return None

async def run(system):
    preds = await asyncio.gather(*[one(r['text'], system) for r in rows])
    bad = sum(1 for p in preds if p is None)
    return {r['id']: p for r, p in zip(rows, preds)}, bad

def score(verdict, bad):
    tp = sum(1 for r in rows if r['expect'] == 'confidential' and verdict[r['id']])
    fn = sum(1 for r in rows if r['expect'] == 'confidential' and verdict[r['id']] is False)
    fp = sum(1 for r in rows if r['expect'] != 'confidential' and verdict[r['id']])
    tn = sum(1 for r in rows if r['expect'] != 'confidential' and verdict[r['id']] is False)
    return {'recall': round(tp / (tp + fn), 4) if tp + fn else None,
            'fpr': round(fp / (fp + tn), 4) if fp + tn else None,
            'tp': tp, 'fn': fn, 'fp': fp, 'tn': tn, 'degraded': bad}

async def main():
    base_v, base_bad = await run(SYSTEM_PROMPT)
    tuned_v, tuned_bad = await run(TUNED_PROMPT)
    fixed = [r['id'] for r in rows if r['expect'] == 'confidential' and base_v[r['id']] is False and tuned_v[r['id']]]
    regressed = [r['id'] for r in rows if r['expect'] == 'confidential' and base_v[r['id']] and tuned_v[r['id']] is False]
    new_fp = [r['id'] for r in rows if r['expect'] != 'confidential' and base_v[r['id']] is False and tuned_v[r['id']]]
    resolved_fp = [r['id'] for r in rows if r['expect'] != 'confidential' and base_v[r['id']] and tuned_v[r['id']] is False]
    # tuned 级联剩余漏报（C=false 且 expect=confidential）
    tuned_c_fn = []
    for r in rows:
        cv = cmap[r['id']]
        stick = session_judged.get(r['id'])
        stick_hit = bool(tuned_v.get(stick)) if stick else False
        if r['expect'] == 'confidential' and not (cv['A'] or (cv['risk'] >= 20 and bool(tuned_v[r['id']])) or stick_hit):
            tuned_c_fn.append(r['id'])
    out = {
        'date': datetime.date.today().isoformat(), 'n': len(rows),
        'service': 'qwen Qwen3-4B-Instruct-2507 (:8002), temperature=0',
        'base_prompt': '内置生产 SYSTEM_PROMPT（small_model.py）',
        'tuned_extra': TUNED_EXTRA,
        'selfcheck': f'cascade 公式以 laya 数据复现 {laya_c_ok}/{len(rows)}',
        'note': 'temperature=0 下 vLLM 仍有浮点边界抖动（同日 base 复跑 82.5% 对 2026-10-29 记录值 81.4%，差 1 条边界样本）；对比以本文件同日两臂为准',
        'base': {'l2_only': score(base_v, base_bad), 'cascade': cascade_scores(base_v), 'items': base_v},
        'tuned': {'l2_only': score(tuned_v, tuned_bad), 'cascade': cascade_scores(tuned_v), 'items': tuned_v,
                  'cascade_fn_ids': tuned_c_fn},
        'diff': {'fixed': fixed, 'regressed': regressed, 'new_fp': new_fp, 'resolved_fp': resolved_fp},
    }
    json.dump(out, open('docs/thesis/qwen-prompt-ab.json', 'w'), ensure_ascii=False, indent=2)
    print(json.dumps({'base_l2': out['base']['l2_only']['recall'], 'base_cascade': out['base']['cascade']['recall'],
                      'tuned_l2': out['tuned']['l2_only']['recall'], 'tuned_cascade': out['tuned']['cascade']['recall'],
                      'base_fpr': out['base']['l2_only']['fpr'], 'tuned_fpr': out['tuned']['l2_only']['fpr'],
                      'base_c_fpr': out['base']['cascade']['fpr'], 'tuned_c_fpr': out['tuned']['cascade']['fpr'],
                      'fixed': len(fixed), 'regressed': len(regressed),
                      'new_fp': len(new_fp), 'resolved_fp': len(resolved_fp),
                      'degraded': [base_bad, tuned_bad]}, ensure_ascii=False))

asyncio.run(main())
