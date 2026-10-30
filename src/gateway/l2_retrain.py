"""L2 影子一键优化管道：人工标注 → 数据集 → 微调 → 候选评测 → 晋升影子。

管理台触发（admin_auth）。全局单 job，并发 409。
stages: dataset → finetune → eval → done/failed。

- 训练与候选评测在 GPU_TRAIN（默认 5，与推理服务同卡；laya 服务本身也固定 5）；
  base=~/models/laya/multilingual。
- 候选评测 = 临时起 laya_l2_server 在 8004（与生产同一推理路径，防渲染漂移）：
  battery（评测集）+ 人工标注全量回归（标过什么就该判什么）+ 历史分歧回归（分歧过的题就该判对）。
- promote = 写 var/laya-l2.env（unit 的 EnvironmentFile，your-user 可写）+
  `sudo -n systemctl restart laya-l2.service`（sudoers 只放行这一条命令）+
  /health 核对 model 身份。权威 backend 切换（qwen→laya）永不走这里，仍人工拍板。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS = os.path.expanduser("~/models")
LAYA_PY = os.path.expanduser("~/venvs/laya/bin/python")
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python")
BASE_MODEL = os.path.join(MODELS, "laya", "multilingual")
DATASET_DIR = os.path.join(MODELS, "laya-dataset")
PROMPT_PROPOSAL_FILE = os.path.join(DATASET_DIR, "prompt-proposals.jsonl")
ENV_FILE = os.path.join(ROOT, "var", "laya-l2.env")
REPORTS_DIR = os.path.join(ROOT, "var", "l2-reports")  # 训练报告落盘（vN.json），重启不丢，供回归diff
EVAL_URL = f"http://127.0.0.1:{int(os.getenv('AI_GATEWAY_L2_RETRAIN_EVAL_PORT', '8004'))}"
LIVE_URL = "http://127.0.0.1:8003"
MIN_LABELS = int(os.getenv("AI_GATEWAY_L2_RETRAIN_MIN_LABELS", "10"))
# 训练/候选评测用卡（推理服务 laya-l2.service 固定 GPU5；2026-09-28 起训练也上 5，整套收一张卡）
TRAIN_GPU = os.getenv("AI_GATEWAY_L2_TRAIN_GPU", "5")

JOB: dict = {"state": "idle", "stage": "", "started_at": 0.0, "log": [],
             "candidate": None, "report": None, "error": ""}
_LOCK = threading.Lock()


def versions() -> dict:
    """盘上可用 checkpoint 版本（降序）+ 当前影子版本（读 var/laya-l2.env）。"""
    vs = []
    try:
        for name in os.listdir(MODELS):
            m = re.fullmatch(r"laya-finetuned-confidential-v(\d+)", name)
            if m and os.path.isdir(os.path.join(MODELS, name)):
                vs.append(int(m.group(1)))
    except OSError:
        pass
    cur = None
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                if line.startswith("LAYA_MODEL_PATH="):
                    m = re.search(r"v(\d+)\s*$", line.strip())
                    if m:
                        cur = int(m.group(1))
    except OSError:
        pass
    return {"versions": sorted(vs, reverse=True), "current": cur}


def list_reports() -> dict:
    """盘上评估报告（vN.json，版本升序）：管理台「版本评估」查看路径。

    job.done 的候选报告只在内存里（网关重启即失）；盘上报告是每版三门
    （battery/现影子对照/标注回归/分歧回归）+ 相对上一版 diff 的持久历史。"""
    out = []
    try:
        for n in os.listdir(REPORTS_DIR):
            if re.fullmatch(r"v\d+\.json", n):
                try:
                    with open(os.path.join(REPORTS_DIR, n), encoding="utf-8") as f:
                        x = json.load(f)
                    # 必须是带 version 的对象：合法 JSON 的数组/字符串会让下面的
                    # sort(x.get(...)) 抛 AttributeError 拖垮整个端点
                    if isinstance(x, dict) and x.get("version") is not None:
                        out.append(x)
                except Exception:
                    continue
    except OSError:
        pass
    out.sort(key=lambda x: x.get("version") or 0)
    return {"reports": out}


def status() -> dict:
    from src.gateway.admin_store import get_admin_store
    out = dict(JOB, log_tail=list(JOB["log"])[-40:])
    out["elapsed_s"] = int(time.time() - JOB["started_at"]) if JOB["started_at"] else 0
    out["min_labels"] = MIN_LABELS
    try:
        out["labeled_count"] = len(get_admin_store().list_l2_labels(2000))
    except Exception:
        out["labeled_count"] = -1
    return out


def _log(stage: str, line: str) -> None:
    JOB["log"].append(f"[{stage}] {str(line).strip()[:2000]}")
    if len(JOB["log"]) > 2000:
        del JOB["log"][:1000]


def _next_version() -> int:
    n = 0
    try:
        for name in os.listdir(MODELS):
            m = re.fullmatch(r"laya-finetuned-confidential-v(\d+)", name)
            if m:
                n = max(n, int(m.group(1)))
    except OSError:
        pass
    return n + 1


def start() -> dict:
    """启动优化训练。返回 422/409 语义的异常由调用方翻译。"""
    from src.gateway.admin_store import get_admin_store
    labeled = get_admin_store().list_l2_labels(2000)
    if len(labeled) < MIN_LABELS:
        raise ValueError(f"人工标注不足：{len(labeled)}/{MIN_LABELS} 条（L2 影子页分歧表标注）")
    if not os.path.isdir(BASE_MODEL):
        raise FileNotFoundError(f"base 模型缺失：{BASE_MODEL}")
    if not os.path.exists(LAYA_PY):
        raise FileNotFoundError(f"laya venv 缺失：{LAYA_PY}")
    with _LOCK:
        if JOB["state"] == "running":
            raise RuntimeError("已有优化任务在跑")
        JOB.update({"state": "running", "stage": "dataset", "started_at": time.time(),
                    "log": [], "candidate": f"{MODELS}/laya-finetuned-confidential-v{_next_version()}",
                    "report": None, "error": ""})
    t = threading.Thread(target=_run, args=(JOB["candidate"],), daemon=True)
    t.start()
    return status()


def _run(candidate: str) -> None:
    try:
        _log("dataset", f"构建数据集（含人工标注）: {VENV_PY} scripts/build_laya_dataset.py")
        r = subprocess.run([VENV_PY, "scripts/build_laya_dataset.py"], cwd=ROOT,
                           capture_output=True, text=True, timeout=900)
        _log("dataset", r.stdout[-1500:])
        if r.returncode != 0:
            raise RuntimeError(f"数据集构建失败 rc={r.returncode}: {r.stderr[-500:]}")
        JOB["stage"] = "finetune"
        _log("finetune", f"GPU{TRAIN_GPU} 训练 → {candidate}")
        r = subprocess.run([LAYA_PY, "scripts/laya_finetune_local.py", BASE_MODEL, DATASET_DIR, candidate],
                           cwd=ROOT, capture_output=True, text=True, timeout=10800,
                           env={**os.environ, "CUDA_VISIBLE_DEVICES": TRAIN_GPU})
        _log("finetune", r.stdout[-1500:])
        if r.returncode != 0:
            raise RuntimeError(f"训练失败 rc={r.returncode}: {r.stderr[-500:]}")
        JOB["stage"] = "eval"
        _log("eval", f"候选服务临时起于 {EVAL_URL}（同生产推理路径）")
        JOB["report"] = _eval_candidate(candidate)
        _attach_regression_diff(candidate)
        JOB["state"] = "done"
        rp = JOB["report"]
        _log("eval", json.dumps({
            "battery": {k: rp["battery"][k] for k in ("total", "correct")},
            "baseline": {k: rp["baseline"][k] for k in ("total", "correct")} if rp.get("baseline") else None,
            "labeled": rp.get("labeled"),
            "divergences": {k: rp["divergences"][k] for k in ("total", "matched", "unmatched")}
            if rp.get("divergences") else None,
            "regressions": rp.get("regressions"),
            "prompt": rp.get("prompt")}, ensure_ascii=False))
    except Exception as e:
        JOB["state"] = "failed"
        JOB["error"] = str(e)[:800]
        _log("error", str(e)[:800])


def _wait_health(url: str, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            h = json.load(urllib.request.urlopen(url + "/health", timeout=3))
            if h.get("ok"):
                return
        except Exception:
            time.sleep(2)
    raise RuntimeError(f"{url} {timeout_s:.0f}s 内未就绪")


def _classify(url: str, text: str, filename: str = "") -> float:
    body = json.dumps({"text": text, "filename": filename}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/classify", data=body,
                                 headers={"Content-Type": "application/json"})
    return float(json.load(urllib.request.urlopen(req, timeout=120))["probabilities"]["CONFIDENTIAL"])


def _load_script_module(mod_name: str, filename: str):
    """importlib 直载 scripts/ 下的独立脚本。脚本内的 sibling import
    （如 laya_eval_common）要求 scripts/ 在 sys.path 上，运行脚本时自动有、
    被 exec 时没有——这里补一次（幂等），两处 loader 共用。"""
    scripts = os.path.join(ROOT, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(scripts, filename))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _collect_cases() -> list:
    """评测电池（与 scripts/eval_laya_live.py 同一电池）：(cat, name, text, filename, want)。
    cat=probe 灰区探针 / real 真实留出 / reg 历史回归。"""
    m = _load_script_module("eval_laya_live", "eval_laya_live.py")
    cases = [("probe", n, t, "", w) for n, t, w in m.PROBES]
    with open(os.path.expanduser("~/models/laya-dataset/real_eval.jsonl"), encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            cases.append(("real", r["source"][:30], r["text"], r.get("filename", ""), r["label"]))
    cases += [("reg", n, t, "", w) for n, t, w in m.REG]
    return cases


def _assemble(scored: list) -> dict:
    """scored: [(cat, name, text, filename, want, p|None, err|None)] → 报告同形。"""
    cats: dict = {}
    wrong = []
    correct = 0
    for cat, name, t, fn, w, p, err in scored:
        got = (1 if p >= 0.7 else 0) if p is not None else None
        c = cats.setdefault(cat, {"total": 0, "correct": 0})
        c["total"] += 1
        if err is None and got == w:
            correct += 1
            c["correct"] += 1
        else:
            wrong.append({"name": f"{cat}:{name}", "want": w, "got": got, "p": round(p, 3) if p is not None else None,
                          "preview": (t or "")[:120].replace("\n", " ") if err is None else f"ERR {err}",
                          "text": t or "", "filename": fn or ""})
    return {"total": len(scored), "correct": correct, "cats": cats, "wrong": wrong}


def _score(url: str, cases: list) -> dict:
    """同一电池打某个服务：总数 + 分项 + 错题明细（name/want/got/p/120字预览+全文）。"""
    return _assemble([(cat, name, t, fn, w, _classify(url, t, fn), None)
                      for cat, name, t, fn, w in cases])


def _score_qwen(cases: list, system: str) -> dict:
    """同一电池打 qwen（候选 system prompt）：直调 _call_qwen，
    bypass 缓存/熔断/影子——纯测 prompt，不污染生产、不触发影子流量。"""
    import asyncio as _aio
    from src.gateway import small_model as _sm

    async def _run():
        out = []
        for cat, name, t, fn, w in cases:
            try:
                res = await _sm._call_qwen(t, fn, [], [], system=system, temperature=0.0)
                if res.get("degraded"):
                    out.append((cat, name, t, fn, w, None, str(res.get("reason", "degraded"))[:100]))
                    continue
                conf = float(res.get("confidence", 0.5))
                out.append((cat, name, t, fn, w,
                            conf if res.get("label") == "CONFIDENTIAL" else 1 - conf, None))
            except Exception as e:
                out.append((cat, name, t, fn, w, None, str(e)[:100]))
        return out

    return _assemble(_aio.run(_run()))


def _ensure_overrides() -> None:
    """量 prompt 前先把持久化覆盖应用进 env。apply_overrides 只在 main.py startup 跑，
    独立脚本/ad-hoc 进程直接读 get_system_prompt() 会拿到内置默认（392 字）而不是线上值
    —— 2026-09-28 实测踩过：评的 prompt 和线上的不是一回事。"""
    from src.gateway import l2_overrides as _ov
    _ov.apply_overrides()


def current_prompt_info() -> dict:
    """当前生效 qwen system prompt 版本戳（报告 provenance：这版 battery 是哪版 prompt 下测的）。"""
    from src.gateway import small_model as _sm
    _ensure_overrides()
    sp = _sm.get_system_prompt() or ""
    return {"sha1": hashlib.sha1(sp.encode()).hexdigest()[:12], "chars": len(sp)}


def prompt_test(system_prompt: str) -> dict:
    """候选 system prompt 打分：同 battery 打 qwen，不落地（~30-60s）。
    通过后再 PUT /admin/api/l2-prompts 应用（即时生效 + 落盘）。"""
    _ensure_overrides()
    sp = system_prompt or ""
    if len(sp.strip()) < 20:
        raise ValueError("system_prompt 太短（<20 字）")
    if len(sp) > 8000:
        raise ValueError("system_prompt 超长（>8000 字）")
    cases = _collect_cases()
    scored = _score_qwen(cases, sp)
    if scored["total"] and len(scored["wrong"]) == scored["total"] and all(
            (w.get("preview") or "").startswith("ERR") for w in scored["wrong"]):
        raise RuntimeError("qwen 全失败（服务不可达/熔断），本次打分无效")
    scored["prompt_sha1"] = hashlib.sha1(sp.encode()).hexdigest()[:12]
    return scored


PROPOSE_META_SYSTEM = """你是企业网关 L2 机密分类器的提示词修补器。管理员给你当前 system prompt
和它判错的真实案例（人工标注为最高优先级 ground truth，want=1 机密/want=0 非密，got 是当前 prompt 的误判）。
只允许追加 1-3 条判定规则：不准修改现有文字，不准放宽已有规则，不准复述案例原文（防对个例过拟合）。
输出严格 JSON：{"patch": "<追加的规则文字，中文，面向分类器>", "rationale": "<一句话说明>"}，不要输出其他内容。"""
PROPOSE_MIN_CASES = 3
PROPOSE_MAX_CASES = 12


def _qwen_wrong_cases(hours: int = 168, max_cases: int = PROPOSE_MAX_CASES) -> list:
    """qwen 主判错题：审计窗内已标且 label != 主 verdict。返回 [(text, filename, want, got)]，去重。"""
    import datetime as _dt
    from src.gateway.admin_store import get_admin_store
    from src.gateway.audit_store import get_audit_store
    since = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - _dt.timedelta(hours=hours)
    rows = get_audit_store().query_mysql(since=since, limit=5000) or []
    labels = get_admin_store().get_l2_labels([r.get("req_id") for r in rows if r.get("req_id")])
    out, seen = [], set()
    for r in rows:
        lab = labels.get(r.get("req_id"))
        l2 = r.get("l2") if isinstance(r.get("l2"), dict) else None
        ml = (l2 or {}).get("label")
        if lab is None or ml not in ("CONFIDENTIAL", "NORMAL"):
            continue
        got = 1 if ml == "CONFIDENTIAL" else 0
        if got == lab:
            continue
        norm = re.sub(r"\s+", " ", str(r.get("text_preview") or "")).strip()
        digest = hashlib.sha1(norm.encode()).hexdigest()
        if len(norm) < 10 or digest in seen:
            continue
        seen.add(digest)
        out.append({"text": norm[:500], "filename": r.get("filename") or "",
                    "want": lab, "got": got})
        if len(out) >= max_cases:
            break
    return out


def _apply_patch(current: str, patch: str) -> str:
    """候选 prompt = 现状 + 追加补丁；补丁已存在则原样返回（防连点叠加）。"""
    p = (patch or "").strip()
    if not p:
        return current
    if p in (current or ""):
        return current
    return (current or "").rstrip() + "\n" + p


async def _call_qwen_free(system: str, user: str) -> str:
    """直调 qwen chat 做自由生成（学 rule_ai）：不套 L2 分类模板，只给 system+user。
    纯诊断/生成用，不进缓存不碰熔断。失败直接抛。"""
    from src.gateway import small_model as _sm
    client = _sm._get_client()
    resp = await client.post(_sm.l2_url(), json={
        "model": _sm.l2_name(),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": 500,
    }, timeout=120.0)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"] or ""


# ---------------- 分歧样本：内网模型自动标注（只出草稿，写库仍走人工 /label） ----------------
# l2_labels 是训练 ground truth（build_laya_dataset 里人标最高优先级），所以模型判断
# 只作为「建议」返回给页面：人工点「采纳」才写库。模型判错直接落库 = 往后每版都带毒。
AUTO_LABEL_SYSTEM = (
    "你是数据安全分级审核员。判断给定待审文本是否包含公司机密或敏感信息"
    "（财务报表、薪酬人事、客户名单、密钥凭据、并购合同、未公开经营数据等）。"
    "只输出一行 JSON，不要任何解释："
    '{"label": 1, "confidence": 0.9, "reason": "不超过20字"}。'
    "label=1 表示机密（禁止出境），label=0 表示普通内容。"
)


async def _auto_label_one(client, url: str, model: str, headers: dict, item: dict) -> dict:
    text = (item.get("text") or "").strip()
    fn = (item.get("filename") or "").strip()
    user = (f"文件名：{fn}\n\n{text}" if fn else text)[:4000]
    resp = await client.post(url, headers=headers, timeout=90.0, json={
        "model": model,
        "messages": [{"role": "system", "content": AUTO_LABEL_SYSTEM},
                     {"role": "user", "content": user}],
        "temperature": 0.0,
        "max_tokens": 300,
        # Qwen3 系（vllm_local/qwen2.5:7b）默认开思考：思考把 token 预算吃光后 content 是
        # 空串（实测 finish=length、completion=200、content=''），整行判不出标签。关思考直出 JSON。
        # 模板不认这个 kwarg 的模型会忽略它，无副作用。
        "chat_template_kwargs": {"enable_thinking": False},
    })
    resp.raise_for_status()
    choice = resp.json()["choices"][0]
    raw = choice["message"]["content"] or ""
    # raw_decode 取第一个完整 JSON 对象：模型常在 JSON 后补说明文字，贪婪正则会把尾随文字一起吞进来
    obj: dict = {}
    i = raw.find("{")
    if i >= 0:
        try:
            obj = json.JSONDecoder().raw_decode(raw[i:])[0]
        except json.JSONDecodeError:
            obj = {}
    if obj.get("label") is None:
        # 模型偶发吐畸形前缀（实测 '{"label":  "label": 0, ...}'）——整段解不动时按字段正则兜底
        m = re.search(r'"label"\s*:\s*"?([01])"?', raw)
        if m:
            c = re.search(r'"confidence"\s*:\s*([0-9.]+)', raw)
            r = re.search(r'"reason"\s*:\s*"([^"]*)"', raw)
            obj = {"label": m.group(1), "confidence": c.group(1) if c else 0,
                   "reason": r.group(1) if r else ""}
    try:
        label = int(obj.get("label"))
    except (TypeError, ValueError):
        label = None
    out = {"req_id": item["req_id"],
           "label": label if label in (0, 1) else None,
           "confidence": round(float(obj.get("confidence") or 0), 3),
           "reason": str(obj.get("reason") or "")[:60]}
    if out["label"] is None:
        out["error"] = (f"模型输出无法解析（finish={choice.get('finish_reason')}，"
                        f"content={len(raw)} 字）")
    return out


def auto_label(items: list[dict], provider, concurrency: int = 4) -> list[dict]:
    """并发打内网 chat 模型给分歧样本出标注草稿（**不写 l2_labels**）。
    单条失败只记 error，不让整批失败；返回顺序与入参一致。"""
    import asyncio as _aio
    import httpx

    url = provider.endpoint("chat")
    if not url:
        raise RuntimeError(f"provider {provider.name} 缺 base_url")
    headers = provider.headers()
    model = provider.model_for("chat") or provider.name

    async def run():
        sem = _aio.Semaphore(concurrency)
        async with httpx.AsyncClient() as client:
            async def one(it):
                async with sem:
                    try:
                        return await _auto_label_one(client, url, model, headers, it)
                    except Exception as e:  # 单条超时/解析失败不影响其余
                        return {"req_id": it["req_id"], "error": repr(e)[:120]}
            return await _aio.gather(*[one(i) for i in items])

    return _aio.run(run())


def latest_prompt_fix() -> dict:
    """最近一次候选 prompt 修补：盘上持久化，重启/切页不丢。无记录返回 {}。"""
    try:
        with open(PROMPT_PROPOSAL_FILE, encoding="utf-8") as f:
            last = None
            for line in f:
                line = line.strip()
                if line:
                    last = line
        return json.loads(last) if last else {}
    except (OSError, json.JSONDecodeError):
        return {}


def propose_prompt_fix() -> dict:
    """qwen 自省修 prompt：主判错题 → qwen 自己提追加补丁 → 同 battery 打基线+候选。
    只生成不应用（~2min，两次 battery）；应用仍走 PUT /l2-prompts 人工确认。
    结果追加落盘 PROMPT_PROPOSAL_FILE（~/models 不入库）后返回。"""
    import asyncio as _aio
    from src.gateway import small_model as _sm
    _ensure_overrides()
    cases = _qwen_wrong_cases()
    if len(cases) < PROPOSE_MIN_CASES:
        raise ValueError(f"主判错题仅 {len(cases)} 条（需 ≥{PROPOSE_MIN_CASES} 条已标且与主判相反），先去分歧表标几条")
    current = _sm.get_system_prompt() or ""
    lines = [f"- want={c['want']} got={c['got']} {c['text']}" + (f"（文件：{c['filename']}）" if c["filename"] else "")
             for c in cases]
    user = ("当前 system prompt 全文：\n" + current + "\n\n判错案例（want 为人工标注真相，got 为当前误判）：\n"
            + "\n".join(lines))
    try:
        raw = _aio.run(_call_qwen_free(PROPOSE_META_SYSTEM, user))
    except Exception as e:
        raise RuntimeError(f"qwen 自省调用失败：{e}") from e
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        obj = json.loads(m.group(0)) if m else {}
    except Exception:
        obj = {}
    patch = str(obj.get("patch") or "").strip()
    if len(patch) < 10:
        raise RuntimeError(f"qwen 未给出可用补丁：{raw[:200]}")
    candidate = _apply_patch(current, patch)
    base = prompt_test(current)
    cand = prompt_test(candidate)
    record = {
        "patch": patch,
        "rationale": str(obj.get("rationale") or "")[:200],
        "cases_used": len(cases),
        "baseline": {"correct": base["correct"], "total": base["total"],
                      "sha1": base.get("prompt_sha1")},
        "candidate": {"correct": cand["correct"], "total": cand["total"],
                       "sha1": cand.get("prompt_sha1"),
                       "wrong": [{k: w[k] for k in ("name", "want", "got", "p")} for w in cand["wrong"]]},
        "candidate_prompt": candidate,
        "ts": int(time.time()),
    }
    # 持久化失败不能吞掉已生成的候选：落盘异常只附加在返回里，不阻断人工应用
    try:
        os.makedirs(DATASET_DIR, exist_ok=True)
        with open(PROMPT_PROPOSAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        record["persist_error"] = repr(e)[:200]
    return record


def add_hard(items) -> dict:
    """错题一键入 hard.jsonl（下版训练自动加权）：按 want 落盘，不确定的别点。
    去重（sha1）、拦短文本（<10 字）、拦非法 label。文件在 ~/models，永不入库。"""
    import re as _re
    path = os.path.join(DATASET_DIR, "hard.jsonl")
    seen = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    h = json.loads(line)
                    seen.add(hashlib.sha1(
                        _re.sub(r"\s+", " ", h.get("text", "")).strip().encode()).hexdigest())
                except Exception:
                    continue
    except OSError:
        pass
    added = skipped = 0
    fresh = []
    os.makedirs(DATASET_DIR, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for it in (items or [])[:200]:
            label = it.get("label")
            norm = _re.sub(r"\s+", " ", str(it.get("text") or "")).strip()
            digest = hashlib.sha1(norm.encode()).hexdigest()
            if label not in (0, 1) or len(norm) < 10 or digest in seen:
                skipped += 1
                continue
            f.write(json.dumps({"text": norm, "label": int(label),
                                "filename": it.get("filename", "")}, ensure_ascii=False) + "\n")
            seen.add(digest)
            added += 1
            fresh.append((norm, int(label)))
    # 联动分歧表：同文本的审计行顺手标上（同一判断一个账本；text_preview 截断过，前缀匹配）
    labeled = 0
    if fresh:
        try:
            from src.gateway.admin_store import get_admin_store
            from src.gateway.audit_store import get_audit_store
            rows = get_audit_store().query_mysql(limit=5000) or []
            store = get_admin_store()
            for r in rows:
                rp = _re.sub(r"\s+", " ", str(r.get("text_preview") or "")).strip()
                if len(rp) < 20:
                    continue
                for norm, label in fresh:
                    if norm == rp or (len(rp) < len(norm) and norm.startswith(rp)):
                        store.upsert_l2_label(r.get("req_id"), label)
                        labeled += 1
                        break
        except Exception as e:
            print(f"[l2_retrain] add_hard 联动标注失败（hard 已落盘，不影响）: {e!r}")
    return {"added": added, "skipped": skipped, "labeled": labeled}


def _labeled_regression(url: str) -> dict:
    """人工标注全量回归：标过什么就该判什么（飞轮的直接度量）。
    只统计审计库窗口内能取到原文的标注（total=可评测数），miss 列明细。
    按文本去重（灌数多轮会把同一句打上多条标注，8 条 miss 其实常是 2 句 × N 轮；
    数据集侧 extract_labeled 也按 sha1 去重，度量口径与之一致）。"""
    from src.gateway.admin_store import get_admin_store
    from src.gateway.audit_store import get_audit_store
    rows = get_audit_store().query_mysql(limit=5000) or []
    by_id = {r.get("req_id"): r for r in rows}
    groups: dict = {}
    for lb in get_admin_store().list_l2_labels(2000):
        r = by_id.get(lb.get("req_id"))
        if not r or not r.get("text_preview"):
            continue
        norm = re.sub(r"\s+", " ", str(r["text_preview"])).strip()
        d = hashlib.sha1(norm.encode()).hexdigest()
        g = groups.get(d)
        if g is None:
            want = int(lb["label"])
            p = _classify(url, norm, r.get("filename") or "")
            got = 1 if p >= 0.7 else 0
            g = groups[d] = {"text": norm[:80], "want": want, "got": got,
                             "p": round(p, 3), "ok": got == want, "req_ids": []}
        g["req_ids"].append(lb.get("req_id"))
    n = len(groups)
    missed = [{"req_id": g["req_ids"][0], "dup": len(g["req_ids"]), "text": g["text"],
               "want": g["want"], "got": g["got"], "p": g["p"]}
              for g in groups.values() if not g["ok"]]
    return {"total": n, "matched": n - len(missed), "missed": missed}


def _candidate_wants() -> list:
    """灌数候选表 [(text, want)]：分歧过的题判没判对的唯一外部标准。
    灌数脚本模块级只有常量 + load_dotenv，import 无副作用。"""
    mod = _load_script_module("_gray_flood", "gray_flood.py")
    return list(mod.CANDIDATES)


def _divergence_regression(url: str) -> dict:
    """历史分歧回归：分歧过的题，候选给出的 want 就该判对（与标注回归互补）。
    v22 施工图误报 43/43+15/15 全绿照样晋升、靠灌数才暴露 → 补这块：
    题目来自审计里 label != shadow_label 的历史行（分歧定义），按文本 sha1 去重；
    text_preview 200 字截断 → 前缀匹配 CANDIDATES 拿 want，匹配不上计 unmatched。"""
    from src.gateway.audit_store import get_audit_store
    wants = _candidate_wants()
    groups: dict = {}
    unmatched = 0
    for r in get_audit_store().query_mysql(limit=5000) or []:
        if not r.get("text_preview"):
            continue
        l2 = r.get("l2")
        if isinstance(l2, str):
            try:
                l2 = json.loads(l2)
            except Exception:
                continue
        if not isinstance(l2, dict):
            continue
        lp, sp = l2.get("label"), l2.get("shadow_label")
        if not lp or not sp or lp == sp:      # 只考"分歧过的题"
            continue
        norm = re.sub(r"\s+", " ", str(r["text_preview"])).strip()
        want = next((w for t, w in wants if norm.startswith(t[:16])), None)
        if want is None:
            unmatched += 1
            continue
        d = hashlib.sha1(norm.encode()).hexdigest()
        if d in groups:
            groups[d]["dup"] += 1
            continue
        p = _classify(url, norm, r.get("filename") or "")
        got = 1 if p >= 0.7 else 0
        groups[d] = {"text": norm[:80], "want": want, "got": got,
                     "p": round(p, 3), "ok": got == want, "dup": 1}
    missed = [g for g in groups.values() if not g["ok"]]
    return {"total": len(groups), "matched": len(groups) - len(missed),
            "missed": missed, "unmatched": unmatched}


def _eval_candidate(candidate: str) -> dict:
    env = {**os.environ, "LAYA_MODEL_PATH": candidate,
           "LAYA_PORT": str(int(os.getenv("AI_GATEWAY_L2_RETRAIN_EVAL_PORT", "8004"))),
           "CUDA_VISIBLE_DEVICES": TRAIN_GPU}
    proc = subprocess.Popen([LAYA_PY, "scripts/laya_l2_server.py"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 300
        up = False
        while time.time() < deadline:
            if proc.poll() is not None:
                tail = proc.stdout.read()[-500:] if proc.stdout else ""
                raise RuntimeError(f"候选服务提前退出: {tail}")
            try:
                if json.load(urllib.request.urlopen(EVAL_URL + "/health", timeout=3)).get("ok"):
                    up = True
                    break
            except Exception:
                time.sleep(3)
        if not up:
            raise RuntimeError("候选服务 300s 内未就绪")
        cases = _collect_cases()
        battery = _score(EVAL_URL, cases)
        # 同一电池跑一遍现影子（8003 生产服务，直调 /classify 不经网关、不进审计）当基线：
        # 候选不如现版就别晋升
        baseline = _score(LIVE_URL, cases)
        return {"battery": battery, "baseline": baseline,
                "labeled": _labeled_regression(EVAL_URL),
                "divergences": _divergence_regression(EVAL_URL),
                "prompt": current_prompt_info()}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except Exception:
            proc.kill()


def _diff_reports(prev: dict, cur: dict) -> dict:
    """两版 battery 错题集对比（按 wrong 名）：新错/修好/仍错。回归 = 上版对、这版错。"""
    def bad(b):
        return {w["name"] for w in (b or {}).get("wrong", [])}
    p = bad((prev or {}).get("battery"))
    c = bad((cur or {}).get("battery"))
    return {"new": sorted(c - p), "fixed": sorted(p - c), "still": sorted(c & p)}


def _attach_regression_diff(candidate: str) -> None:
    """本版报告落盘 + 与上一版报告 diff。无历史版则跳过。"""
    m = re.search(r"v(\d+)$", candidate or "")
    ver = int(m.group(1)) if m else 0
    prev = None
    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        ids = sorted(int(n[1:-5]) for n in os.listdir(REPORTS_DIR)
                     if re.fullmatch(r"v\d+\.json", n))
        ids = [i for i in ids if i < ver]
        if ids:
            with open(os.path.join(REPORTS_DIR, f"v{ids[-1]}.json"), encoding="utf-8") as f:
                prev = json.load(f).get("report")
    except Exception as e:
        _log("eval", f"历史报告读取失败（跳过回归diff）: {e!r}")
    if prev is not None:
        JOB["report"]["regressions"] = {"against": ids[-1], **_diff_reports(prev, JOB["report"])}
    try:
        with open(os.path.join(REPORTS_DIR, f"v{ver}.json"), "w", encoding="utf-8") as f:
            json.dump({"version": ver, "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                       "report": JOB["report"]}, f, ensure_ascii=False)
    except Exception as e:
        _log("eval", f"报告落盘失败（不影响本次结果）: {e!r}")


def promote(version: int) -> dict:
    """候选 checkpoint 晋升为影子模型：写 env 文件 + 无密重启 laya-l2 + 身份核对。"""
    with _LOCK:
        if JOB["state"] == "running":
            raise RuntimeError("优化任务在跑，稍后再晋升")
    name = f"laya-finetuned-confidential-v{int(version)}"
    candidate = os.path.join(MODELS, name)
    if not re.fullmatch(r"laya-finetuned-confidential-v\d+", name) or not os.path.isdir(candidate):
        raise FileNotFoundError(f"checkpoint 不存在：{candidate}")
    os.makedirs(os.path.dirname(ENV_FILE), exist_ok=True)
    tmp = ENV_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(f"LAYA_MODEL_PATH={candidate}\n")
    os.replace(tmp, ENV_FILE)
    r = subprocess.run(["sudo", "-n", "systemctl", "restart", "laya-l2.service"],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"laya-l2 重启失败: {r.stderr.strip()[-300:]}")
    _wait_health(LIVE_URL, 180)
    h = json.load(urllib.request.urlopen(LIVE_URL + "/health", timeout=5))
    if h.get("model") != name:
        raise RuntimeError(f"身份核对失败：服务跑的是 {h.get('model')}，期望 {name}")
    return {"ok": True, "model": name, "env_file": ENV_FILE}
