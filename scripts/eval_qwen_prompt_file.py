# -*- coding: utf-8 -*-
"""候选 L2 system prompt 评测：pilot 184（仅 L2 / 级联 / 干净 161）+ real_eval 103。

用法: .venv/bin/python scripts/eval_qwen_prompt_file.py <prompt.txt> [tag]
输出: docs/thesis/qwen-prompt-candidate.json（覆盖写，tag 区分轮次）

只做离线评测：直调 :8002，不写生产审计库、不改 l2_overrides.yaml。
"""
import asyncio, json, os, sys
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT); sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, 'scripts'))
load_dotenv(os.path.join(ROOT, '.env'))
from src.gateway.small_model import _call_qwen  # noqa: E402
import l2_candidate_common as C  # noqa: E402

path = sys.argv[1] if len(sys.argv) > 1 else 'docs/thesis/l2-prompt-candidate-v1.txt'
tag = sys.argv[2] if len(sys.argv) > 2 else os.path.basename(path)
PROMPT = open(path, encoding='utf-8').read().strip()
assert len(PROMPT) > 200

pilot = [json.loads(l) for l in open('tests/fixtures/pilot_dataset.jsonl', encoding='utf-8') if l.strip()]
real = [json.loads(l) for l in open(os.path.expanduser('~/models/laya-dataset/real_eval.jsonl'), encoding='utf-8') if l.strip()]
assert len(pilot) == 184 and len(real) == 103


def overlap_ids():
    tset = [json.loads(l)["text"].strip()
            for l in open(os.path.expanduser('~/models/laya-dataset/train.jsonl'), encoding='utf-8') if l.strip()]
    out = set()
    for r in pilot:
        pt = r["text"].strip()
        if len(pt) >= 8 and any(pt in t or t in pt for t in tset):
            out.add(r["id"])
    return out


sem = asyncio.Semaphore(8)


async def one(text):
    async with sem:
        try:
            out = await _call_qwen(text, system=PROMPT, temperature=0.0)
            return out.get('label') == 'CONFIDENTIAL'
        except Exception:
            return None


def confusion(labels, preds):
    tp = sum(1 for l, p in zip(labels, preds) if l and p)
    fn = sum(1 for l, p in zip(labels, preds) if l and not p)
    fp = sum(1 for l, p in zip(labels, preds) if not l and p)
    tn = sum(1 for l, p in zip(labels, preds) if not l and not p)
    return {'tp': tp, 'fn': fn, 'fp': fp, 'tn': tn,
            'recall': round(tp / (tp + fn), 4) if tp + fn else None,
            'fpr': round(fp / (fp + tn), 4) if fp + tn else None,
            'acc': round((tp + tn) / len(labels), 4)}


def real_key(r, i):
    return f"{r['source'][:28]}|{r['text'][:20]}#{i}"


async def main():
    pilot_preds = await asyncio.gather(*[one(r['text']) for r in pilot])
    real_preds = await asyncio.gather(*[one(r['text']) for r in real])
    bad = sum(1 for p in pilot_preds + list(real_preds) if p is None)

    cbase = {it['id']: it for it in json.load(open('docs/thesis/cascade-baseline.json'))['items']}
    pmap = {r['id']: (1.0 if v else 0.0) for r, v in zip(pilot, pilot_preds)}
    l2_full, casc_full = C.pilot_metrics(pilot, cbase, pmap)
    overlap = overlap_ids()
    assert len(overlap) == 23, f'干净 161 剔除数应为 23，实得 {len(overlap)}'
    clean = [r for r in pilot if r['id'] not in overlap]
    l2_clean, casc_clean = C.pilot_metrics(clean, cbase, pmap)

    r_lab = [str(r['label']) == '1' for r in real]
    real_cm = confusion(r_lab, [bool(x) for x in real_preds])

    fn_pilot = [r for r, p in zip(pilot, pilot_preds) if r['expect'] == 'confidential' and not p]
    fp_pilot = [r for r, p in zip(pilot, pilot_preds) if r['expect'] != 'confidential' and p]
    fn_real = [r for r, p, l in zip(real, real_preds, r_lab) if l and not p]
    fp_real = [r for r, p, l in zip(real, real_preds, r_lab) if (not l) and p]

    out = {
        'tag': tag, 'prompt_len': len(PROMPT), 'prompt_sha12': __import__('hashlib').sha1(PROMPT.encode()).hexdigest()[:12],
        'pilot': {'l2_full184': l2_full, 'cascade_full184': casc_full,
                  'l2_clean161': l2_clean, 'cascade_clean161': casc_clean},
        'real_eval': real_cm,
        'errors': {
            'pilot_fn': [[r['id'], r['text'][:80]] for r in fn_pilot],
            'pilot_fp': [[r['id'], r['text'][:80]] for r in fp_pilot],
            'real_fn': [[r['source'][:20], r['text'][:80]] for r in fn_real],
            'real_fp': [[r['source'][:20], r['text'][:80]] for r in fp_real],
        },
        'p_pilot': pmap,
        'p_real': {real_key(r, i): (1.0 if p else 0.0) for i, (r, p) in enumerate(zip(real, real_preds))},
        'bad': bad,
    }
    json.dump(out, open('docs/thesis/qwen-prompt-candidate.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)

    print(f"tag={tag} len={len(PROMPT)} bad={bad}")
    print(f"pilot  L2 全184 {l2_full['recall']}/{l2_full['fpr']}  级联 {casc_full['recall']}/{casc_full['fpr']}")
    print(f"pilot  干净161 L2 {l2_clean['recall']}/{l2_clean['fpr']}  级联 {casc_clean['recall']}/{casc_clean['fpr']}")
    print(f"real   acc {real_cm['acc']} recall {real_cm['recall']} fpr {real_cm['fpr']} {real_cm['tp']}/{real_cm['fn']}/{real_cm['fp']}/{real_cm['tn']}")
    print('pilot FN:', [(r['id'], r['text'][:40]) for r in fn_pilot])
    print('pilot FP:', [(r['id'], r['text'][:40]) for r in fp_pilot])
    print('real  FN:', [(r['text'][:40],) for r in fn_real])
    print('real  FP:', [(r['text'][:40],) for r in fp_real])


asyncio.run(main())
