"""生产 L2（qwen :8002）在 pilot 184 例上的复跑，与 laya 结果并列。

用法: .venv/bin/python scripts/eval_qwen_pilot.py
输出: docs/thesis/qwen-baseline.json
说明: 走生产 system prompt 与 :8002 vLLM chat 口；temperature=0 去采样噪声。
"""
import asyncio, json, os, sys
from dotenv import load_dotenv
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT); sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT,'.env'))
from src.gateway.small_model import _call_qwen
rows=[json.loads(l) for l in open('tests/fixtures/pilot_dataset.jsonl',encoding='utf-8') if l.strip()]
sem=asyncio.Semaphore(8)
async def one(r):
    async with sem:
        try:
            out=await _call_qwen(r['text'], temperature=0.0)
            return out.get('label')=='CONFIDENTIAL'
        except Exception as e:
            return None
async def main():
    preds=await asyncio.gather(*[one(r) for r in rows])
    bad=sum(1 for p in preds if p is None)
    tp=sum(1 for r,p in zip(rows,preds) if r['expect']=='confidential' and p)
    fn=sum(1 for r,p in zip(rows,preds) if r['expect']=='confidential' and p is False)
    fp=sum(1 for r,p in zip(rows,preds) if r['expect']!='confidential' and p)
    tn=sum(1 for r,p in zip(rows,preds) if r['expect']!='confidential' and p is False)
    print(json.dumps({'n':len(rows),'degraded':bad,'recall':round(tp/(tp+fn),4) if tp+fn else None,
                      'fpr':round(fp/(fp+tn),4) if fp+tn else None,'tp':tp,'fn':fn,'fp':fp,'tn':tn}))
    json.dump({'n':len(rows),'degraded':bad,'recall':round(tp/(tp+fn),4),'fpr':round(fp/(fp+tn),4),
               'tp':tp,'fn':fn,'fp':fp,'tn':tn,'items':[{'id':r['id'],'expect':r['expect'],'qwen':p} for r,p in zip(rows,preds)]},
              open('docs/thesis/qwen-baseline.json','w'),ensure_ascii=False,indent=2)
asyncio.run(main())
