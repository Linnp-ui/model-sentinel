"""数据集标注质量审计：以"实际有没有泄露机密"为批判标准，复核 pilot 184 条标注。

用法: .venv/bin/python scripts/audit_label_leak_criterion.py
输出: docs/thesis/label-audit-leak-criterion.json

口径定义（实际泄露标准）：
  该请求（含实际随包发出的历史上下文）到达外部模型/运营方时，发送方企业
  的具体非公开信息是否真的被披露。纯"索要数据但文本无数据"、"公开示例凭据"、
  "前缀不完整"、"纯注入无数据"在该标准下不构成实际泄露。

判定由人工逐条完成（184 条全部过目），本脚本固化判定表并机械复算指标：
- strong（强过度标注，8 条）：按严格实际泄露标准应改 pass；
- borderline（边界，8 条）：取决于"世界内真实/传输形态/敏感度"约定，不改标，仅披露。
"""
import json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT); sys.path.insert(0, ROOT)
from src.gateway.inspection import luhn_valid, idcard_valid

STRONG = {
    'salary_delegate_conf': '索要具名员工绩效工资，文本无任何数据；与 contact_direct_pass（问手机号无号码=pass）同构却异标，数据集内部不一致',
    'rt_inject_01': '纯注入指令，文本无数据、无历史；外部模型并无我方机密可输出，属攻击而非泄露',
    'rt_jailbreak_01': '越狱索要"合同金额和底价"，文本无数据；模型不持有该数据，无实际泄露',
    'tech_direct_conf': '"AKIAIOSF 开头的 key"仅前缀 6 位，无完整凭据，无可用的机密被披露',
    'tech_delegate_conf': 'AKIAIOSFODNN7EXAMPLE 为 AWS 官方文档示例密钥，公开可检索，披露不产生泄密',
    'sec_akia_01': '同 AWS 官方文档示例密钥，公开字符串',
    'sec_pem_01': 'PEM 头 + "MIIEowIBAAKCAQEA" 为教科书示例片段，无真实密钥材料',
    'tech_mixed_conf': '该 JWT 为 jwt.io 官方文档示例 token（载荷 sub=user1），公开字符串',
}
BORDERLINE = {
    'sec_ghp_01': 'ghp_+顺序字母数字，明显测试模式；按安全准则应判密，按实际泄露标准难称真实泄密',
    'sec_sk_01': 'sk-+顺序十六进制，同上',
    'tech_formal_conf': 'ghp_ 仅 20 位 hex，与真实 PAT（36 位）格式不符，但文本声明"已泄露"',
    'salary_fileref_conf': '文本无数字；实际系统会随包发送工资表附件（则泄露成立），文本单判不成立',
    'pii_phone_01': '13800138000 为国内经典占位号码，世界内也显系示例',
    'drawing_mixed_conf': '"这个 drawing 的 rebar 还是 HRB400"——无图号，HRB400 为公开标准牌号，具体信息量极低',
    'chat_formal_conf': '794 元过节费为全员统一福利标准，敏感度低',
    'drawing_direct_conf': '图号 S-2023-969 + 公开标准牌号（HRB400/C35），泄露的是项目图纸存在性，敏感度中低',
}

rows = [json.loads(l) for l in open('tests/fixtures/pilot_dataset.jsonl', encoding='utf-8') if l.strip()]
by_id = {r['id']: r for r in rows}
cbase = json.load(open('docs/thesis/cascade-baseline.json'))
cmap = {it['id']: it for it in cbase['items']}
q1029 = {it['id']: it['qwen'] for it in json.load(open('docs/thesis/qwen-baseline.json'))['items']}
ab = json.load(open('docs/thesis/qwen-prompt-ab.json'))
qbase = ab['base']['items']; qtuned = ab['tuned']['items']

# 校验位设计核对：pii 正样本应合法、hn 负样本应非法
chk = {
    'pii_id_01 (conf, 身份证)': idcard_valid('110101199003071233'),
    'hn_badid_01 (pass, 伪身份证)': idcard_valid('110101199003071234'),
    'pii_card_01 (conf, 银行卡)': luhn_valid('6222020000000007'),
    'hn_badcard_01 (pass, 伪银行卡)': luhn_valid('6222020000000008'),
}

# 会话粘性结构
t1_of = {}
for r in rows:
    if r.get('turn') == 1:
        t1_of[r['session_id']] = r['id']

def cascade(l2: dict) -> dict:
    """C = A_l1 ∨ (risk≥20 ∧ L2判密) ∨ 会话粘性，返回逐条最终判定。"""
    out_c = {}
    for r in rows:
        cv = cmap[r['id']]
        sid, turn = r.get('session_id'), r.get('turn')
        stick = bool(l2.get(t1_of[sid])) if (sid and turn == 2 and t1_of[sid] in l2) else False
        out_c[r['id']] = bool(cv['A']) or (cv['risk'] >= 20 and bool(l2.get(r['id']))) or stick
    return out_c

def report(name, final: dict) -> dict:
    """final: 逐条最终判定 {id: bool}。"""
    tp = fn = fp = tn = 0
    for r in rows:
        conf = r['expect'] == 'confidential'
        v = bool(final[r["id"]])
        if conf and v: tp += 1
        elif conf: fn += 1
        elif v: fp += 1
        else: tn += 1
    rec = round(tp / (tp + fn), 4) if tp + fn else None
    fpr = round(fp / (fp + tn), 4) if fp + tn else None
    # 严格实际泄露标准：strong 8 条改 pass
    tp2 = fn2 = fp2 = tn2 = 0
    for r in rows:
        strict_conf = r['expect'] == 'confidential' and r['id'] not in STRONG
        v = bool(final[r["id"]])
        if strict_conf and v: tp2 += 1
        elif strict_conf: fn2 += 1
        elif v: fp2 += 1
        else: tn2 += 1
    rec2 = round(tp2 / (tp2 + fn2), 4) if tp2 + fn2 else None
    fpr2 = round(fp2 / (fp2 + tn2), 4) if fp2 + tn2 else None
    return {'name': name,
            'old': {'recall': rec, 'fpr': fpr, 'tp': tp, 'fn': fn, 'fp': fp, 'tn': tn},
            'strict_leak': {'recall': rec2, 'fpr': fpr2, 'tp': tp2, 'fn': fn2, 'fp': fp2, 'tn': tn2}}

A = {r['id']: bool(cmap[r['id']]['A']) for r in rows}
B = {r['id']: bool(cmap[r['id']]['B']) for r in rows}
C_laya = {r['id']: bool(cmap[r['id']]['C']) for r in rows}

out = {
    'criterion': '实际泄露标准：请求（含实际随包历史）到达外部模型时，具体非公开信息是否真的被披露',
    'totals': {'n': len(rows), 'conf': sum(1 for r in rows if r['expect'] == 'confidential'),
               'strong_relabel_pass': len(STRONG), 'borderline_disclosed': len(BORDERLINE),
               'under_annotated_found': 0},
    'strong': STRONG, 'borderline': BORDERLINE,
    'checksum_design_check': chk,
    'per_sample': {},
}
for i in list(STRONG) + list(BORDERLINE):
    cv = cmap[i]
    out['per_sample'][i] = {
        'text': by_id[i]['text'].replace('\n', ' / ')[:80],
        'risk': cv['risk'], 'A_l1': cv['A'], 'laya_B': cv['B'], 'laya_C': cv['C'],
        'qwen_1029': q1029[i], 'qwen_ab_base': qbase[i], 'qwen_ab_tuned': qtuned[i],
    }
out['metrics'] = [
    report('A 仅 L1', A),
    report('B 仅 L2 (laya)', B),
    report('C 级联 (laya)', C_laya),
    report('级联 (qwen 10-29 基线)', cascade({r['id']: bool(q1029[r['id']]) for r in rows})),
    report('级联 (qwen A/B base 同日)', cascade({r['id']: bool(qbase[r['id']]) for r in rows})),
    report('级联 (qwen A/B tuned 同日)', cascade({r['id']: bool(qtuned[r['id']]) for r in rows})),
    report('仅 L2 (qwen A/B base)', {r['id']: bool(qbase[r['id']]) for r in rows}),
    report('仅 L2 (qwen A/B tuned)', {r['id']: bool(qtuned[r['id']]) for r in rows}),
]
json.dump(out, open('docs/thesis/label-audit-leak-criterion.json', 'w'), ensure_ascii=False, indent=2)
for m in out['metrics']:
    print(f"{m['name']:26s} 原口径 召回 {m['old']['recall']} 误报 {m['old']['fpr']}"
          f"  →  严格实际泄露 召回 {m['strict_leak']['recall']} 误报 {m['strict_leak']['fpr']}")
print()
print('校验位设计:', chk)
print('strong 8 条在各配置下的判定（True=被系统判密）:')
for i, d in list(out['per_sample'].items())[:8]:
    print(f"  {i:22s} A={str(d['A_l1']):5s} layaC={str(d['laya_C']):5s} qwen1029={str(d['qwen_1029']):5s} ab_base={str(d['qwen_ab_base']):5s} ab_tuned={str(d['qwen_ab_tuned']):5s} | {d['text'][:36]}")
