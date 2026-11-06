# -*- coding: utf-8 -*-
"""qwen 生产现役提示词（l2_overrides.yaml 持久化值）评测：pilot 184 + real_eval 103。

用法: .venv/bin/python scripts/eval_qwen_prod_prompt.py
输出: docs/thesis/qwen-prod-prompt.json

背景: 2026-10-29 生产 L2 提示词被改为 l2_overrides.yaml 的手调版（注入段重写 + 4 条尾部
条款），而论文 §6.5.1 的 10-29 基线与 A/B base 臂走的是代码内置默认（579 字），两者不同。
本脚本：① 补测生产现役版在 pilot 的仅 L2/级联指标（全 184 与干净 161 双口径）；
② real_eval 同时跑 内置默认 / tuned / 生产现役 三臂，与既有 qwen 结果逐条比对，
钉死 78.6%/80.6% 的提示词口径。
"""
import asyncio, json, os, re, sys
import yaml
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT); sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, 'scripts'))
load_dotenv(os.path.join(ROOT, '.env'))
from src.gateway.small_model import _call_qwen, SYSTEM_PROMPT
import l2_candidate_common as C

PROD_PROMPT = yaml.safe_load(open('l2_overrides.yaml', encoding='utf-8'))['AI_GATEWAY_L2_SYSTEM_PROMPT']
_src = open('scripts/eval_qwen_prompt_ab.py', encoding='utf-8').read()
_m = re.search(r'TUNED_EXTRA = """(.*?)"""', _src, re.S)
assert _m, 'TUNED_EXTRA 未找到'
TUNED_PROMPT = SYSTEM_PROMPT.replace('只输出JSON', _m.group(1) + '\n\n只输出JSON')
assert TUNED_PROMPT != SYSTEM_PROMPT

pilot = [json.loads(l) for l in open('tests/fixtures/pilot_dataset.jsonl', encoding='utf-8') if l.strip()]
real = [json.loads(l) for l in open(os.path.expanduser('~/models/laya-dataset/real_eval.jsonl'), encoding='utf-8') if l.strip()]
assert len(pilot) == 184 and len(real) == 103

def overlap_ids():
    """同 check_train_pilot_overlap：pilot 与训练集文字双向包含（最短 8 字）。"""
    tset = [json.loads(l)["text"].strip()
            for l in open(os.path.expanduser('~/models/laya-dataset/train.jsonl'), encoding='utf-8') if l.strip()]
    out = set()
    for r in pilot:
        pt = r["text"].strip()
        if len(pt) >= 8 and any(pt in t or t in pt for t in tset):
            out.add(r["id"])
    return out

sem = asyncio.Semaphore(8)
async def one(text, system):
    async with sem:
        try:
            out = await _call_qwen(text, system=system, temperature=0.0)
            return out.get('label') == 'CONFIDENTIAL'
        except Exception:
            return None

async def run(rows, system):
    return await asyncio.gather(*[one(r['text'], system) for r in rows])

def real_key(r, i):
    return f"{r['source'][:28]}|{r['text'][:20]}#{i}"

def confusion(labels, preds):
    tp = sum(1 for l, p in zip(labels, preds) if l and p)
    fn = sum(1 for l, p in zip(labels, preds) if l and not p)
    fp = sum(1 for l, p in zip(labels, preds) if not l and p)
    tn = sum(1 for l, p in zip(labels, preds) if not l and not p)
    return {'tp': tp, 'fn': fn, 'fp': fp, 'tn': tn,
            'recall': round(tp / (tp + fn), 4) if tp + fn else None,
            'fpr': round(fp / (fp + tn), 4) if fp + tn else None,
            'acc': round((tp + tn) / len(labels), 4)}

async def main():
    pilot_preds = await run(pilot, PROD_PROMPT)
    real_base = await run(real, SYSTEM_PROMPT)
    real_tuned = await run(real, TUNED_PROMPT)
    real_prod = await run(real, PROD_PROMPT)

    cbase = {it['id']: it for it in json.load(open('docs/thesis/cascade-baseline.json'))['items']}
    pmap = {r['id']: (1.0 if v else 0.0) for r, v in zip(pilot, pilot_preds)}
    l2_full, casc_full = C.pilot_metrics(pilot, cbase, pmap)
    overlap = overlap_ids()
    assert len(overlap) == 23, f'干净 161 剔除数应为 23，实得 {len(overlap)}'
    clean = [r for r in pilot if r['id'] not in overlap]
    assert len(clean) == 161
    l2_clean, casc_clean = C.pilot_metrics(clean, cbase, pmap)

    r_lab = [str(r['label']) == '1' for r in real]
    rec = {}
    for name in ('zeroshot', 'tuned'):
        d = json.load(open(f'docs/thesis/eval/l2-candidate-qwen-{name}-realeval.json'))
        rec[name] = d['real_eval']['p']
    keys = [real_key(r, i) for i, r in enumerate(real)]
    assert set(keys) == set(rec['zeroshot']) == set(rec['tuned']), 'real_eval 键映射不一致'
    rec_z = {k: rec['zeroshot'][k] >= 0.7 for k in keys}
    rec_t = {k: rec['tuned'][k] >= 0.7 for k in keys}
    verdict_base = {k: bool(p) for k, p in zip(keys, real_base)}
    verdict_tuned = {k: bool(p) for k, p in zip(keys, real_tuned)}
    verdict_prod = {k: bool(p) for k, p in zip(keys, real_prod)}

    out = {
        'date': '2026-11-05',
        'prompt_len': {'builtin': len(SYSTEM_PROMPT), 'tuned': len(TUNED_PROMPT), 'prod': len(PROD_PROMPT)},
        'pilot_prod': {
            'l2_full184': l2_full, 'cascade_full184': casc_full,
            'l2_clean161': l2_clean, 'cascade_clean161': casc_clean,
            'overlap_ids': sorted(overlap),
        },
        'real_eval': {
            'base': confusion(r_lab, [bool(x) for x in real_base]),
            'tuned': confusion(r_lab, [bool(x) for x in real_tuned]),
            'prod': confusion(r_lab, [bool(x) for x in real_prod]),
        },
        'caliber_check': {
            'recorded_zeroshot_78.6': {
                'vs_base_mismatch': sum(1 for k in keys if rec_z[k] != verdict_base[k]),
                'vs_prod_mismatch': sum(1 for k in keys if rec_z[k] != verdict_prod[k]),
            },
            'recorded_tuned_80.6': {
                'vs_tuned_mismatch': sum(1 for k in keys if rec_t[k] != verdict_tuned[k]),
                'vs_prod_mismatch': sum(1 for k in keys if rec_t[k] != verdict_prod[k]),
            },
        },
        'p_prod_pilot': pmap,
        'p_prod_realeval': {k: (1.0 if v else 0.0) for k, v in verdict_prod.items()},
    }
    json.dump(out, open('docs/thesis/qwen-prod-prompt.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k not in ('p_prod_pilot', 'p_prod_realeval')},
                     ensure_ascii=False, indent=1))

asyncio.run(main())
