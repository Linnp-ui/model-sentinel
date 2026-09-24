"""Admin Console API routes (M0/M1).

挂在 /admin/api 下，由 main.py include 时统一套 admin_auth 依赖（401 JSON，
不走 admin_page_auth 的 302）。所有方法用 sync def，FastAPI 自动进线程池，
不阻塞事件循环。

端点一览：
  GET    /admin/api/meta                     后端模式/凭据状态/计数
  POST   /admin/api/password                 1.4 修改管理员密码（改后全 session 失效）
  GET    /admin/api/keys                     4.2 key 映射列表（脱敏）
  POST   /admin/api/keys                     登记新 key
  POST   /admin/api/keys/import               批量导入（每行 名称,key 逗号分隔，逐行结果）
  PATCH  /admin/api/keys/{kid}               改名称/owner/备注/停用
  DELETE /admin/api/keys/{kid}
  GET    /admin/api/keyrules?kind=           2 KEY 黑白名单列表（key 掩码显示）
  POST   /admin/api/keyrules                 新增（kind=black|white，key 原文精确匹配）
  DELETE /admin/api/keyrules/{rid}
  GET    /admin/api/ip/{ip}/verdict          调试用：查某 IP 当前判定

M2：
  GET    /admin/api/model-policy             1.1/1.2/1.3 模型策略（含 env 只读的审查模型信息）
  PUT    /admin/api/model-policy             整体保存（Pydantic 校验，失败 422 不落盘）
  POST   /admin/api/model-policy/fallback  运行时 fallback 开关（即时生效，重启后恢复 env）
  GET    /admin/api/policies                 5.1 L1 规则列表
  POST   /admin/api/policies                 新增规则
  PUT    /admin/api/policies/{name}          更新规则（可重命名：body 里带 new_name）
  DELETE /admin/api/policies/{name}
  POST   /admin/api/policies/test            L1 规则测试（text/filename/size -> 命中详情）
  POST   /admin/api/policies/build-when      5.2 表单语义 -> when 条件（前端预览用）
  POST   /admin/api/l2/generate-rule         5.2 匹配项 AI 生成（只建议不落库）
  POST   /admin/api/l2/test                  5.3 L2 规则测试（待识别内容 -> 审查判定）

M5（别名候选选择）：
  GET    /admin/api/providers                provider 注册表（脱敏，不含密钥）
  GET    /admin/api/providers/{name}/models  代理拉取供应商 /models（管理员选模型用）

M6（运维留痕）：
"""
from __future__ import annotations

import os
import re
import time
from typing import Dict, List, Optional, Union

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.gateway.admin_store import get_admin_store, mask_key
from src.gateway import l2_overrides as _l2ov

router = APIRouter(prefix="/admin/api")


# ---------------- helpers ----------------

def store_session_secret() -> Optional[str]:
    """给 main.py 的 _admin_secret() 用：DB 凭据存在时返回派生密钥。"""
    st = get_admin_store()
    return st.session_secret() if st.has_credential() else None


def verify_admin_password(pw: str) -> bool:
    """给 main.py 登录校验用：DB 凭据优先，env 兜底。"""
    return get_admin_store().verify_admin_password(pw)


def key_verdict(key: str) -> Optional[str]:
    """给 main.py 的 KEY 过滤中间件用：white/black/None。"""
    return get_admin_store().lookup_key(key)


# ---------------- models ----------------

class PasswordChangeReq(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)


class ApiKeyCreateReq(BaseModel):
    key: str = Field(min_length=8, max_length=256)
    name: str = Field(min_length=1, max_length=128)
    owner: str = Field(default="", max_length=128)
    note: str = Field(default="", max_length=2048)


class ApiKeyGenReq(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    owner: str = Field(default="", max_length=128)
    note: str = Field(default="", max_length=2048)


class ApiKeyUpdateReq(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    owner: Optional[str] = Field(default=None, max_length=128)
    note: Optional[str] = Field(default=None, max_length=2048)
    disabled: Optional[bool] = None


class KeyRuleCreateReq(BaseModel):
    kind: str = Field(pattern="^(black|white)$")
    key_value: str = Field(default="", max_length=256)
    key_name: str = Field(default="", max_length=128)
    note: str = Field(default="", max_length=512)


# ---------------- meta ----------------

@router.get("/meta")
def get_meta():
    st = get_admin_store()
    return {
        "backend": st.backend,
        "credential_set": st.has_credential(),
        "api_keys": len(st.list_api_keys()),
        "key_rules": len(st.list_key_rules()),
        "request_logs": st.count_request_logs(),
    }


# ---------------- 1.4 密码 ----------------

@router.post("/password")
def change_password(body: PasswordChangeReq):
    st = get_admin_store()
    if not verify_admin_password(body.old_password):
        raise HTTPException(403, "old password incorrect")
    if body.old_password == body.new_password:
        raise HTTPException(422, "new password must differ from old")
    try:
        st.set_admin_password(body.new_password)
    except ValueError as e:
        raise HTTPException(422, str(e))
    # session 密钥随哈希轮换，旧 cookie 全部失效；客户端需重新登录
    return {"ok": True, "relogin": True, "backend": st.backend}


# ---------------- 4.2 API KEY ----------------

@router.get("/keys")
def list_keys():
    return {"items": get_admin_store().list_api_keys()}


@router.post("/keys")
def create_key(body: ApiKeyCreateReq):
    try:
        item = get_admin_store().create_api_key(body.key, body.name, body.owner, body.note)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "item": item}


@router.post("/keys/generate")
def generate_key(body: ApiKeyGenReq):
    """Generate a random API key server-side. The full key is returned once in
    item.key_plain; it is never retrievable afterward (only masked form stored)."""
    try:
        item = get_admin_store().generate_api_key(body.name, body.owner, body.note)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "item": item}


class KeysImportReq(BaseModel):
    text: str


@router.post("/keys/import")
def import_keys_api(body: KeysImportReq):
    """批量导入：每行 名称,key 逗号分隔（全角逗号兼容），# 起头为注释。逐行返回结果，响应不含完整 key。"""
    results = []
    added = 0
    for idx, raw in enumerate(body.text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:
            name, key = line.split(",", 1)
        elif "，" in line:
            name, key = line.split("，", 1)
        else:
            results.append({"line": idx, "key_masked": "", "status": "invalid", "message": "缺少逗号分隔"})
            continue
        key = key.strip()
        name = name.strip()
        if name.startswith("sk-") or name.startswith("pk_"):
            results.append({"line": idx, "key_masked": "", "status": "invalid", "message": "第 1 段疑似 KEY，格式应为 名称,key"})
            continue
        if len(key) < 8:
            results.append({"line": idx, "key_masked": (key if "*" in key else mask_key(key)), "status": "invalid", "message": "key 至少 8 位"})
            continue
        if not name:
            results.append({"line": idx, "key_masked": (key if "*" in key else mask_key(key)), "status": "invalid", "message": "缺少名称"})
            continue
        if len(name) > 128:
            results.append({"line": idx, "key_masked": (key if "*" in key else mask_key(key)), "status": "invalid", "message": "名称过长"})
            continue
        try:
            get_admin_store().create_api_key(key, name)
            results.append({"line": idx, "key_masked": (key if "*" in key else mask_key(key)), "status": "ok", "message": ""})
            added += 1
        except ValueError as e:
            msg = str(e)
            status = "duplicate" if "already registered" in msg else "invalid"
            results.append({"line": idx, "key_masked": (key if "*" in key else mask_key(key)), "status": status, "message": msg})
    if not results:
        raise HTTPException(422, "no lines to import")
    return {"ok": True, "added": added, "results": results}


@router.patch("/keys/{kid}")
def update_key(kid: int, body: ApiKeyUpdateReq):
    try:
        get_admin_store().update_api_key(
            kid, name=body.name, owner=body.owner, note=body.note, disabled=body.disabled
        )
    except KeyError:
        raise HTTPException(404, "key not found")
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True}


@router.delete("/keys/{kid}")
def delete_key(kid: int):
    get_admin_store().delete_api_key(kid)
    return {"ok": True, "deleted": kid}


# ---------------- 2 KEY 黑白名单 ----------------

@router.get("/keyrules")
def list_keyrules(kind: Optional[str] = None):
    if kind and kind not in ("black", "white"):
        raise HTTPException(422, "kind must be black|white")
    items = get_admin_store().list_key_rules(kind)
    for it in items:
        it["key_masked"] = mask_key(it.pop("key_value", ""))
    return {"items": items}


@router.post("/keyrules")
def create_keyrule(body: KeyRuleCreateReq):
    kv = (body.key_value or "").strip()
    if not kv and body.key_name.strip():
        # 按登记名解析明文（管理页下拉用；明文不出后端）
        kv = {k["name"]: k["key_plain"] for k in get_admin_store().list_api_keys_raw()}.get(body.key_name.strip(), "")
        if not kv:
            raise HTTPException(422, "key_name not registered")
    if not (8 <= len(kv) <= 256):
        raise HTTPException(422, "key_value must be 8-256 chars")
    try:
        item = get_admin_store().add_key_rule(body.kind, kv, body.note)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "item": item}


@router.delete("/keyrules/{rid}")
def delete_keyrule(rid: int):
    get_admin_store().remove_key_rule(rid)
    return {"ok": True, "deleted": rid}


@router.get("/keyrules/verdict")
def key_verdict_api(key: str):
    return {"key_masked": mask_key(key), "verdict": get_admin_store().lookup_key(key)}


# ---------------- M3 统计与建议 ----------------

from src.gateway import stats_service as _stats


def _win(hours: float) -> tuple:
    return _stats._window(hours)


@router.get("/stats/overview")
def stats_overview_api(hours: float = 168.0):
    """3.1/3.2 汇总卡：总调用 / 拦截 / 活跃 IP / 平均耗时。"""
    store = get_admin_store()
    since, until = _win(hours)
    return store.stats_overview(since, until)


@router.get("/stats/group")
def stats_group_api(dim: str = "model", hours: float = 168.0):
    """按维度分组（model|key_name|client_ip|provider|action|rule）。

    rule = request_log.blocked_reason（命中规则名；空=放行无规则）。
    """
    if dim not in ("model", "key_name", "client_ip", "provider", "action", "rule"):
        raise HTTPException(422, "dim must be one of model/key_name/client_ip/provider/action/rule")
    store = get_admin_store()
    since, until = _win(hours)
    col = "blocked_reason" if dim == "rule" else dim
    return {"dim": dim, "hours": hours, "items": store.stats_group(since, until, col)}


@router.get("/stats/billing")
def stats_billing_api(hours: float = 168.0):
    """Token 计费：KEY × 实际模型 × 单价 → ¥（内部成本分摊口径，不等于上游账单）。"""
    store = get_admin_store()
    return _stats.build_billing(store, hours)


@router.get("/stats/timeseries")
def stats_timeseries_api(hours: float = 168.0):
    """60 桶趋势（统计页时间趋势；request_log 源，与同页 KPI 卡同口径）。"""
    store = get_admin_store()
    since, until = _win(hours)
    return store.stats_timeseries(since, until)


@router.get("/stats/chart")
def stats_chart_api(hours: float = 24.0):
    """总览折线图（QPS+延迟分位；request_log 源按窗口重分桶，点数与环线相当）。"""
    store = get_admin_store()
    since, until = _win(hours)
    return store.stats_chart_series(since, until)




@router.get("/stats/key-model")
def stats_key_model_api(hours: float = 168.0, top: int = 20):
    """按 KEY(人员) × 模型聚合 token（统计可视化页 KEY×模型占比环图用）。"""
    store = get_admin_store()
    since, until = _win(hours)
    return {"hours": hours, "items": store.stats_key_model(since, until, top)}


@router.get("/stats/summary")
def stats_summary_api(hours: float = 168.0, top: int = 20):
    """统计页整页单请求（单遍聚合，7 端点合并；口径与各自端点逐字段一致，见 tests）。"""
    store = get_admin_store()
    return _stats.build_summary(store, hours, top=top)




@router.get("/logs")
def list_logs_api(
    limit: int = 100,
    offset: int = 0,
    ip: str = "",
    key_name: str = "",
    model: str = "",
    action: str = "",
    hours: float = 168.0,
):
    """请求明细分页（新→旧）。"""
    store = get_admin_store()
    since, until = _win(hours)
    return store.list_request_logs(
        limit=limit, offset=offset, ip=ip, key_name=key_name,
        model=model, action=action, since=since, until=until,
    )


@router.post("/logs/cleanup")
def cleanup_logs_api(days: float = 90.0):
    """手动清理超过 days 天的明细。"""
    import time as _t
    cutoff = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(_t.time() - days * 86400))
    return {"deleted": get_admin_store().purge_request_logs(cutoff)}


@router.get("/suggestions")
def suggestions_api(hours: float = 24.0):
    """3.3 调节策略建议（规则引擎）。

    meta 供 UI 明示口径：触发指标 = 异常处置（拦截 ∪ 本地路由）；规则 0/5/6
    用 30 天独立窗口、规则 1/2/3/4/7 用 hours 窗口 —— 同一张卡片上并存两个窗口。
    """
    store = get_admin_store()
    return {
        "hours": hours,
        "items": _stats.build_suggestions(store, hours),
        "meta": _stats.suggestion_meta(hours),
    }


class SuggestionApplyReq(BaseModel):
    kind: str = Field(default="black", pattern="^(black|white|unwhite|reorder|remove_rule|add_limit)$")
    key: str = Field(default="", max_length=256)
    key_name: str = Field(default="", max_length=128)
    rule_id: Optional[int] = None
    rule_a: str = Field(default="", max_length=128)
    rule_b: str = Field(default="", max_length=128)
    order: list = Field(default_factory=list)
    rule_name: str = Field(default="", max_length=128)
    limit_model: str = Field(default="", max_length=256)
    limit_rpm: int = Field(default=0, ge=0, le=1000000)
    note: str = Field(default="", max_length=512)


@router.post("/suggestions/apply")
def suggestion_apply_api(req: SuggestionApplyReq):
    """一键应用分级建议：black/white 按 key 或 key_name（后端解析明文）；unwhite 按 rule_id；
    reorder 按 order 全量重排 L1 规则 priority（规则 4 高频先判一次排到底；
    无 order 时按 rule_a/rule_b 交换，兼容旧建议）；
    remove_rule 按 rule_name 删除一条 L1 规则（规则 5 沉默清理）；
    add_limit 按 limit_model/limit_rpm 加模型全局限流（规则 7，已有则更新，热重载生效）。"""
    store = get_admin_store()
    if req.kind == "add_limit":
        if not req.limit_model or req.limit_rpm <= 0:
            raise HTTPException(422, "limit_model and positive limit_rpm required")
        from src.gateway import model_policy as _mp
        pol = _mp.load_model_policy()
        rows = [m.model_dump() for m in pol.model_limits]
        hit = [m for m in rows if m.get("model") == req.limit_model]
        if hit:
            hit[0]["rpm"] = req.limit_rpm
            hit[0]["enabled"] = True
        else:
            rows.append({"model": req.limit_model, "rpm": req.limit_rpm,
                         "enabled": True, "note": "strategy:suggest"})
        data = pol.model_dump()
        data["model_limits"] = rows
        _mp.save_model_policy(data)
        return {"ok": True, "limited": {req.limit_model: req.limit_rpm}}
    if req.kind == "remove_rule":
        if not req.rule_name:
            raise HTTPException(422, "rule_name required")
        from src.gateway import policy_admin as _pa
        if not _pa.delete_policy(req.rule_name):
            raise HTTPException(404, "policy rule not found")
        return {"ok": True, "removed": req.rule_name}
    if req.kind == "reorder":
        from src.gateway import policy_admin as _pa
        if req.order:
            names = [n for n in dict.fromkeys(req.order) if n]
            if len(names) < 2:
                raise HTTPException(422, "order needs at least 2 distinct rules")
            cur = {p["name"]: p for p in _pa.list_policies()}
            missing = [n for n in names if n not in cur]
            if missing:
                raise HTTPException(404, f"policy rule not found: {missing[0]}")
            slots = sorted(cur[n]["priority"] for n in names)
            done = {}
            for n, p in zip(names, slots):
                row = dict(cur[n])
                row["priority"] = p
                _pa.upsert_policy(row, original_name=n)
                done[n] = p
            return {"ok": True, "reordered": done}
        if not req.rule_a or not req.rule_b or req.rule_a == req.rule_b:
            raise HTTPException(422, "rule_a and rule_b required (distinct)")
        cur = {p["name"]: p for p in _pa.list_policies()}
        if req.rule_a not in cur or req.rule_b not in cur:
            raise HTTPException(404, "policy rule not found")
        ra, rb = dict(cur[req.rule_a]), dict(cur[req.rule_b])
        ra["priority"], rb["priority"] = rb["priority"], ra["priority"]
        _pa.upsert_policy(ra, original_name=req.rule_a)
        _pa.upsert_policy(rb, original_name=req.rule_b)
        return {"ok": True, "reordered": {req.rule_a: ra["priority"],
                                          req.rule_b: rb["priority"]}}
    if req.kind == "unwhite":
        if req.rule_id is None:
            raise HTTPException(422, "rule_id required")
        rows = [r for r in store.list_key_rules() if r["id"] == req.rule_id]
        if not rows or rows[0]["kind"] != "white":
            raise HTTPException(404, "white rule not found")
        if rows[0].get("source") not in ("strategy", "auto"):
            raise HTTPException(409, "manual rule: remove it manually")
        store.remove_key_rule(req.rule_id)
        return {"ok": True, "removed": req.rule_id}
    kv = req.key
    if not kv and req.key_name:
        kv = {k["name"]: k["key_plain"] for k in store.list_api_keys_raw()}.get(req.key_name, "")
    if not kv:
        raise HTTPException(422, "key or key_name not resolvable")
    try:
        r = store.add_key_rule(kind=req.kind, key_value=kv,
                               note=req.note or f"key-tier:{req.kind}", source="strategy")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "rule": {"id": r.get("id"), "kind": r.get("kind"),
                                 "source": "strategy",
                                 "key_masked": mask_key(kv)}}


# ---------------- M2: 1.1/1.2/1.3 模型策略 ----------------

class ModelPolicySaveReq(BaseModel):
    """整体保存 external_candidates / internal_models / model_limits / review_model。"""
    model_config = ConfigDict(extra="allow", protected_namespaces=())

    external_candidates: list = Field(default_factory=list)
    internal_models: list = Field(default_factory=list)
    model_limits: list = Field(default_factory=list)
    load_balance_mode: str = "none"
    review_model: dict = Field(default_factory=dict)


@router.get("/model-policy")
def get_model_policy():
    from src.gateway import model_policy as mp
    pol = mp.load_model_policy()
    return {
        "policy": pol.model_dump(),
        "env_flag": {
            "AI_GATEWAY_MODEL_FALLBACK": os.getenv(
                "AI_GATEWAY_MODEL_FALLBACK", "true"),
        },
    }


@router.put("/model-policy")
def save_model_policy_api(body: ModelPolicySaveReq):
    from src.gateway import model_policy as mp
    try:
        pol = mp.save_model_policy({
            "external_candidates": body.external_candidates,
            "internal_models": body.internal_models,
            "model_limits": body.model_limits,
            "load_balance_mode": body.load_balance_mode,
            "review_model": body.review_model,
        })
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "policy": pol.model_dump()}


class FallbackReq(BaseModel):
    enabled: bool


@router.post("/model-policy/fallback")
def set_fallback_api(body: FallbackReq):
    """运行时开关 AI_GATEWAY_MODEL_FALLBACK：写 os.environ 即时生效（routing 每请求 os.getenv），重启后恢复 env 配置。"""
    os.environ["AI_GATEWAY_MODEL_FALLBACK"] = "true" if body.enabled else "false"
    return {"ok": True, "enabled": body.enabled,
            "effective": os.getenv("AI_GATEWAY_MODEL_FALLBACK", "true")}



# ---------------- M2: 5.1 L1 规则 CRUD ----------------

class PolicyRuleReq(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=64)
    priority: int = Field(default=100, ge=0, le=1000)
    action: str = Field(pattern="^(allow|route_local|block)$")
    description: str = ""
    when: dict | str = "always"
    target: dict | None = None


class PolicyTestReq(BaseModel):
    text: str = ""
    filename: str = ""
    size: int = 0


class BuildWhenReq(BaseModel):
    content_type: str = Field(pattern="^(prompt|filename|file|ocr|size)$")
    match_type: str = Field(pattern="^(regex|contains|size)$")
    match_value: object


class L2GenerateReq(BaseModel):
    description: str = Field(min_length=2, max_length=500)


class L2TestReq(BaseModel):
    text: str = Field(default="", max_length=20000)
    filename: str = ""
    headers: list = Field(default_factory=list)
    sheet_names: list = Field(default_factory=list)


# ---------- 对外模型别名（ext-flash / ext-pro 动态路由） ----------

class AliasMemberReq(BaseModel):
    provider: str
    model: str
    priority: int = 100
    enabled: bool = True


class AliasGroupSaveReq(BaseModel):
    description: str = ""
    members: list[AliasMemberReq]
    new_name: str = ""  # 非空且 ≠ URL 组名 → 整组重命名（撞名 422）


@router.get("/aliases")
def list_alias_groups_api():
    """对外模型别名组列表（含成员/优先级/启用状态）。"""
    return {"groups": get_admin_store().list_alias_groups()}


def build_public_models() -> list:
    """/v1/models 对外模型列表（单一真源）：别名组（有启用成员）+ 内网主力模型
    （model_policy.internal_models 启用项，与别名重名跳过）+ 用户自注册模型。

    main.list_models 与管理端 /models/public 共用，两端保证一致。
    """
    from .model_policy import load_model_policy
    from .user_models import get_store
    data: list = []
    now = int(time.time())
    seen: set = set()
    for g in get_admin_store().list_alias_groups():
        members = [m for m in (g.get("members") or []) if m.get("enabled", 1)]
        if not members:
            continue
        members = sorted(members, key=lambda x: x["priority"])
        default = members[0]
        seen.add(g["name"])
        data.append({
            "id": g["name"],
            "object": "model",
            "created": now,
            "owned_by": "gateway",
            "gateway": {
                "kind": "chat",
                "alias_group": g["name"],
                "default": f"{default['provider']}/{default['model']}",
                "candidates": [f"{m['provider']}/{m['model']}" for m in members],
            },
        })
    try:
        for im in load_model_policy().internal_models:
            if not im.enabled or im.model in seen:
                continue
            seen.add(im.model)
            data.append({
                "id": im.model,
                "object": "model",
                "created": now,
                "owned_by": "gateway",
                "gateway": {
                    "kind": "chat",
                    "local": True,
                    "provider": im.provider,
                    "model": im.model,
                    "configured": True,
                    "note": im.note,
                },
            })
    except Exception:
        pass
    for m in get_store().list():
        mid = m.default_model or m.name
        data.append({
            "id": str(m.name),
            "object": "model",
            "created": int(m.created_at),
            "owned_by": f"user:{m.owner}",
            "gateway": {
                "kind": "chat",
                "local": False,
                "default_model": m.default_model,
                "model": str(mid),
                "configured": True,
                "user_defined": True,
            },
        })
    return data


@router.get("/models/public")
def public_models_api():
    """与 /v1/models 同一构造（build_public_models），供管理台对齐展示。"""
    return {"data": build_public_models()}


@router.put("/aliases/{name}")
def update_alias_group_api(name: str, body: AliasGroupSaveReq):
    """整组保存别名成员（按 priority 升序重排为 10,20,...；最小者为默认）。

    body.new_name 非空且 ≠ URL 组名 → 整组重命名（目标名撞名 422），返回的
    group 为最终名。"""
    try:
        saved = get_admin_store().update_alias_group(
            name,
            [m.model_dump() for m in body.members],
            description=body.description,
            new_name=body.new_name,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    final = (body.new_name or "").strip() or name
    return {"ok": True, "group": final, "routes": saved}


@router.delete("/aliases/{name}")
def delete_alias_group_api(name: str):
    """删除整个别名组（含全部成员）。删除后 /v1/models 不再展示该别名，

    指向它的请求按未知模型处理；组名大小写不敏感，重复删除 404。
    """
    try:
        get_admin_store().delete_alias_group(name)
    except ValueError as e:
        msg = str(e)
        raise HTTPException(404 if "not found" in msg else 422, msg)
    return {"ok": True, "group": name}


# ---------------- 供应商模型列表（别名候选选择用） ----------------

@router.get("/providers")
def list_providers_api():
    """routing.yaml provider 注册表（不含密钥值；base_url 保留 ${VAR} 原始形态，编辑回写不丢 env 占位符）。"""
    from src.gateway import providers as _pv
    from src.gateway.llms.registry import SUPPORTED_MODES
    from src.gateway.providers import load_routing
    cfg = load_routing()
    raw_provs = (_pv.load_raw().get("providers") or {})
    items = []
    for p in cfg.providers.values():
        item = {
            "name": p.name, "base_url": p.base_url, "api_key_env": p.api_key_env,
            "api_mode": p.api_mode, "kind": p.kind, "default_model": p.default_model,
            "embedding_model": p.embedding_model, "timeout": p.timeout,
            "local": p.local, "extra_headers": p.extra_headers or {},
        }
        for k, v in (raw_provs.get(p.name) or {}).items():
            if k != "api_key_value":  # 明文密钥字段永不外泄
                item[k] = v
        item["configured"] = p.configured
        # 展开后的真实 base_url（只读，供 L2 审查卡按模型自动推导端点；编辑仍用 raw 的 base_url）
        item["base_url_resolved"] = p.base_url
        items.append(item)
    return {"providers": items, "api_modes": list(SUPPORTED_MODES)}


class ProviderUpsertReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=4, max_length=512)
    api_key_env: str = Field(default="", max_length=128)
    api_mode: str = Field(default="openai")
    kind: str = Field(default="chat", pattern="^(chat|embedding)$")
    default_model: str = Field(default="", max_length=256)
    embedding_model: str = Field(default="", max_length=256)
    timeout: float = Field(default=60.0, ge=1, le=600)
    local: bool = False
    extra_headers: Optional[Dict[str, str]] = None  # None=未传，更新时不动原值


class ProviderCreateReq(ProviderUpsertReq):
    name: str = Field(min_length=1, max_length=64, pattern="^[A-Za-z0-9_-]+$")


def _provider_payload(body: ProviderUpsertReq) -> Dict[str, Any]:
    d = body.model_dump(exclude_none=True)
    d.pop("name", None)
    return d


def _provider_refs(name: str) -> List[str]:
    """引用该 provider 的位置（删除保护，防把默认路由/候选/别名/策略指到虚空）。"""
    from src.gateway import model_policy as _mp
    from src.gateway import policy_admin as _pa
    from src.gateway.providers import load_routing

    refs: List[str] = []
    cfg = load_routing()
    for field_, val in (("routing.default_external", cfg.default_external),
                        ("routing.default_local", cfg.default_local),
                        ("routing.default_embedding", cfg.default_embedding)):
        if val == name:
            refs.append(f"{field_}")
    pol = _mp.load_model_policy()
    for c in pol.external_candidates:
        if c.provider == name:
            refs.append(f"外部候选 {name}/{c.model}")
    for c in pol.internal_models:
        if c.provider == name:
            refs.append(f"内部模型 {name}/{c.model}")
    for g in get_admin_store().list_alias_groups():
        if any(m.get("provider") == name for m in g.get("members", [])):
            refs.append(f"别名组 {g.get('name')}")
    for rule in _pa.list_policies():
        if ((rule.get("target") or {}) or {}).get("provider") == name:
            refs.append(f"策略规则 {rule.get('name')}")
    seen, out = set(), []
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


@router.post("/providers")
def create_provider_api(request: Request, body: ProviderCreateReq):
    from src.gateway import providers as _pv
    name = body.name.strip()
    raw = _pv.load_raw()
    provs = raw.get("providers") or {}
    if name in provs or _pv.load_routing().get(name) is not None:
        raise HTTPException(409, f"provider 已存在：{name}")
    provs[name] = _provider_payload(body)
    try:
        _pv.save_providers(provs)
    except ValueError as e:
        raise HTTPException(422, str(e))
    get_admin_store().log_ops("provider_upsert", f"create {name}", _operator(request))
    return {"ok": True, "name": name}


@router.put("/providers/{name}")
def update_provider_api(request: Request, name: str, body: ProviderUpsertReq):
    from src.gateway import providers as _pv
    raw = _pv.load_raw()
    provs = raw.get("providers") or {}
    if name not in provs and _pv.load_routing().get(name) is None:
        raise HTTPException(404, f"provider 不存在：{name}")
    merged = {k: v for k, v in (provs.get(name) or {}).items() if k != "api_key_value"}
    merged.update(_provider_payload(body))
    provs[name] = merged
    try:
        _pv.save_providers(provs)
    except ValueError as e:
        raise HTTPException(422, str(e))
    get_admin_store().log_ops("provider_upsert", f"update {name}", _operator(request))
    return {"ok": True, "name": name}


class ProviderKeyReq(BaseModel):
    value: str = Field(default="", max_length=512)  # 空串 = 清除


@router.put("/providers/{name}/key")
def set_provider_key_api(request: Request, name: str, body: ProviderKeyReq):
    """配置 provider 的 env key：os.environ 即时生效（免重启）+ 落 .env 持久化。

    值只写不读：任何端点都不返回明文（GET /providers 只有 api_key_env 变量名 + configured 布尔）；
    ops-log 只记变量名+长度。"""
    from src.gateway import env_file
    from src.gateway import providers as _pv
    prov = _pv.load_routing().get(name)
    if prov is None:
        raise HTTPException(404, f"provider 不存在：{name}")
    env_name = (prov.api_key_env or "").strip()
    if not env_name:
        raise HTTPException(409, f"provider {name} 未设 api_key_env（先填 env 变量名再配值）")
    try:
        if body.value.strip():
            r = env_file.set_env_value(env_name, body.value)
            get_admin_store().log_ops("provider_key_set", f"{env_name} len={r['length']}", _operator(request))
        else:
            r = env_file.clear_env_value(env_name)
            get_admin_store().log_ops("provider_key_clear", env_name, _operator(request))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "env": env_name, "configured": env_file.env_value_set(env_name)}


@router.delete("/providers/{name}")
def delete_provider_api(request: Request, name: str):
    from src.gateway import providers as _pv
    raw = _pv.load_raw()
    provs = raw.get("providers") or {}
    if name not in provs and _pv.load_routing().get(name) is None:
        raise HTTPException(404, f"provider 不存在：{name}")
    refs = _provider_refs(name)
    if refs:
        raise HTTPException(409, "仍有引用，先处理再删除：" + "；".join(refs))
    provs.pop(name, None)
    try:
        _pv.save_providers(provs)
    except ValueError as e:
        raise HTTPException(422, str(e))
    get_admin_store().log_ops("provider_delete", name, _operator(request))
    return {"ok": True, "deleted": name}


def _models_list_key(prov, header_key: str = "") -> tuple:
    """拉 /models 的 key 三级解析：请求头 > 专用 env(AI_GATEWAY_MODELS_LIST_KEY_*) > provider env key。
    专用 env 只服务于拉列表/健康检查端点，routing 调用链不读，永不进入实际模型调用。"""
    k = (header_key or "").strip()
    if k:
        return k, "header"
    k = os.getenv("AI_GATEWAY_MODELS_LIST_KEY_" + re.sub(r"[^A-Z0-9]", "_", prov.name.upper()), "").strip()
    if k:
        return k, "models_list_env"
    # provider env key 为**显式**读取：T44 删掉了 Provider.headers("") 的静默回落
    # （未登记身份白吃公司额度的通道），运维端点用网关侧库存凭据是正确做法。
    k = (prov.api_key or "").strip()
    return (k, "provider_env") if k else ("", "none")


@router.get("/providers/{name}/models")
def list_provider_models_api(name: str, x_api_key: Optional[str] = Header(None, alias="X-Api-Key")):
    """代理拉取供应商 GET {base_url}/models —— 别名编辑时供管理员下拉选择。

    上游 4xx/5xx/超时一律 502 透传失败原因，不做静默降级（与网关原则一致）。

    X-Api-Key 头优先于 provider env key（生产常缺某 provider 的 env key，
    管理员自带 key 拉列表；key 只发往该 provider 的 base_url，不落库不记日志）。"""
    import httpx

    from src.gateway.providers import load_routing
    from src.gateway.routing import upstream_proxy
    prov = load_routing().get(name)
    if prov is None:
        raise HTTPException(404, f"未知 provider：{name}")
    base = (prov.base_url or "").rstrip("/")
    if not base:
        raise HTTPException(422, f"provider {name} 未配置 base_url")
    try:
        key, _src = _models_list_key(prov, x_api_key)
        resp = httpx.get(f"{base}/models", headers=prov.headers(key),
                         timeout=min(prov.timeout or 60, 30),
                         trust_env=False, proxy=upstream_proxy())
    except httpx.HTTPError as e:
        raise HTTPException(502, f"拉取 {name} 模型列表失败：{e!r}")
    if resp.status_code != 200:
        raise HTTPException(502, f"上游 {name} /models 返回 HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(502, f"上游 {name} /models 返回非 JSON")
    items = data.get("data") if isinstance(data, dict) else data
    models = []
    for it in items or []:
        if isinstance(it, dict):
            mid = str(it.get("id") or it.get("name") or "").strip()
            if mid:
                models.append({"id": mid, "name": it.get("name") or mid,
                               "context_length": it.get("context_length"),
                               "pricing": it.get("pricing")})
        elif isinstance(it, str) and it.strip():
            models.append({"id": it.strip(), "name": it.strip()})
    return {"provider": name, "count": len(models), "models": models}


# ---------------- 运维操作（带留痕；main.py 旧端点保留但不留痕） ----------------

def _operator(request: Request) -> str:
    from src.gateway.main import _resolve_client_ip
    return _resolve_client_ip(request)


@router.get("/policies")
def list_policies_api():
    from src.gateway import policy_admin
    return {"items": policy_admin.list_policies()}


@router.post("/policies")
def create_policy_api(body: PolicyRuleReq):
    from src.gateway import policy_admin
    try:
        rule = policy_admin.upsert_policy(body.model_dump())
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "rule": rule}


@router.put("/policies/{name}")
def update_policy_api(name: str, body: PolicyRuleReq):
    from src.gateway import policy_admin
    data = body.model_dump()
    new_name = str((data.pop("new_name", None) or "") or name)
    data["name"] = new_name
    try:
        rule = policy_admin.upsert_policy(data, original_name=name)
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "rule": rule}


@router.delete("/policies/{name}")
def delete_policy_api(name: str):
    from src.gateway import policy_admin
    if not policy_admin.delete_policy(name):
        raise HTTPException(404, "rule not found")
    return {"ok": True, "deleted": name}


@router.post("/policies/test")
def test_policy_api(body: PolicyTestReq):
    from src.gateway import policy_admin
    return policy_admin.test_rule(body.text, body.filename, body.size)


@router.post("/policies/build-when")
def build_when_api(body: BuildWhenReq):
    from src.gateway import policy_admin
    try:
        when = policy_admin.build_when(body.content_type, body.match_type, body.match_value)
    except (ValueError, re.error) as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "when": when}


# ---------------- M2: 5.2/5.3 L2 生成与测试 ----------------

@router.post("/l2/generate-rule")
async def l2_generate_rule_api(body: L2GenerateReq):
    """AI 生成匹配项建议（5.2）。只返回建议，不落库；失败由 UI 手动填写兜底。"""
    from src.gateway.rule_ai import generate_rule_suggestion
    try:
        suggestion = await generate_rule_suggestion(body.description)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"ok": True, "suggestion": suggestion}


@router.post("/l2/test")
async def l2_test_api(body: L2TestReq):
    """L2 规则测试（5.3）：待识别内容 -> 审查判定（label/confidence/reason）。"""
    from src.gateway.small_model import classify, is_degraded
    if not body.text.strip() and not body.filename:
        raise HTTPException(422, "text or filename is required")
    result = await classify(body.text, body.filename, body.headers, body.sheet_names)
    # degraded=True 表示 L2 没产出可信判定（超时/坏 JSON/熔断）——
    # 此时 result["label"] 是兜底值，不代表「内容安全」，必须显式告知调用者。
    return {"ok": True, "result": result, "degraded": is_degraded(result)}


class L2ConfigPutReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    SMALL_MODEL_ENABLED: Optional[bool] = None
    SMALL_MODEL_TIMEOUT: Optional[float] = Field(default=None, ge=0.5, le=60)
    SMALL_MODEL_URL: Optional[str] = Field(default=None, max_length=512)
    SMALL_MODEL_NAME: Optional[str] = Field(default=None, max_length=128)
    SMALL_MODEL_MAX_TOKENS: Optional[int] = Field(default=None, ge=8, le=512)
    AI_GATEWAY_L2_BACKEND: Optional[str] = Field(default=None, max_length=16)
    AI_GATEWAY_L2_SHADOW: Optional[bool] = None
    AI_GATEWAY_L2_SHADOW_URL: Optional[str] = Field(default=None, max_length=512)
    AI_GATEWAY_WL_L2_SAMPLE: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD: Optional[Union[int, Dict[str, int]]] = None
    AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD: Optional[Union[int, Dict[str, int]]] = None
    AI_GATEWAY_SUGGEST_ERROR_RATE: Optional[float] = Field(default=None, ge=0.0, le=1.0)


def _ser_window_map(v, lo: int, hi: int, key: str) -> str:
    """按窗口阈值序列化：int=全窗口同值；{hours: value} → '24:20,168:100,720:500'。"""
    pairs = {}
    if isinstance(v, int) and not isinstance(v, bool):
        pairs = {24: v, 168: v, 720: v}
    else:
        for h, x in (v or {}).items():
            try:
                h_i, x_i = int(h), int(x)
            except (TypeError, ValueError):
                raise HTTPException(422, f"{key} 窗口/取值非法")
            if not (lo <= x_i <= hi):
                raise HTTPException(422, f"{key} 取值须 {lo}-{hi}")
            pairs[h_i] = x_i
    if not pairs:
        raise HTTPException(422, f"{key} 不能为空")
    return ",".join(f"{h}:{x}" for h, x in sorted(pairs.items()))


class L2PromptsPutReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 空串表示"还原默认"
    AI_GATEWAY_L2_SYSTEM_PROMPT: Optional[str] = Field(default=None, max_length=8000)
    AI_GATEWAY_L2_USER_PROMPT_TEMPLATE: Optional[str] = Field(default=None, max_length=2000)


@router.get("/l2-config")
def l2_config_get():
    from src.gateway import small_model as _sm
    from src.gateway import stats_service as _st
    from src.gateway.main import wl_l2_sample as _wl
    th = _st._thresholds()
    win3 = lambda m, d: {str(h): _st.pick_window(m, h, d) for h in (24, 168, 720)}
    return {
        "SMALL_MODEL_ENABLED": _sm.l2_enabled(),
        "SMALL_MODEL_TIMEOUT": _sm.l2_timeout(),
        "SMALL_MODEL_URL": _sm.l2_url(),
        "SMALL_MODEL_NAME": _sm.l2_name(),
        "SMALL_MODEL_MAX_TOKENS": _sm.l2_max_tokens(),
        "AI_GATEWAY_L2_BACKEND": _sm.l2_backend(),
        "AI_GATEWAY_L2_SHADOW": (os.getenv("AI_GATEWAY_L2_SHADOW") or "").strip().lower() == "true",
        "AI_GATEWAY_L2_SHADOW_URL": (os.getenv("AI_GATEWAY_L2_SHADOW_URL") or "").strip(),
        "AI_GATEWAY_WL_L2_SAMPLE": _wl(),
        "AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD": win3(th["violation_map"], 20),
        "AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD": win3(th["volume_map"], 1000),
        "AI_GATEWAY_SUGGEST_ERROR_RATE": th["error_rate"],
        # T46: PUT 落盘 l2_overrides.yaml，启动时由 l2_overrides.apply_overrides 应用
        "persisted": _l2ov.load_overrides(),
    }


@router.put("/l2-config")
def l2_config_put(req: L2ConfigPutReq):
    applied = {}
    for k, v in req.model_dump(exclude_unset=True).items():
        if v is None:
            continue
        if k == "AI_GATEWAY_L2_BACKEND" and str(v).strip().lower() not in ("qwen", "laya"):
            raise HTTPException(422, "AI_GATEWAY_L2_BACKEND must be qwen|laya")
        if k == "AI_GATEWAY_L2_BACKEND":
            v = str(v).strip().lower()
        if k == "AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD":
            v = _ser_window_map(v, 1, 1000, k)
        elif k == "AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD":
            v = _ser_window_map(v, 1, 100000, k)
        os.environ[k] = str(v).lower() if isinstance(v, bool) else str(v)
        applied[k] = v
    persisted = _l2ov.save_overrides(applied) if applied else _l2ov.load_overrides()
    return {"ok": True, "applied": applied, "persisted": persisted}


@router.get("/l2-prompts")
def l2_prompts_get():
    """L2 提示词当前值（按优先级：内存覆盖 > 持久化 > env > 内置默认）。"""
    from src.gateway import small_model as _sm
    persisted = _l2ov.load_overrides()
    return {
        "system_prompt": _sm.get_system_prompt(),
        "user_prompt_template": _sm.get_user_prompt_template(),
        "defaults": {
            "system_prompt": _sm.SYSTEM_PROMPT,
            "user_prompt_template": _sm.USER_PROMPT_TEMPLATE,
        },
        "persisted": {
            k: persisted[k] for k in ("AI_GATEWAY_L2_SYSTEM_PROMPT", "AI_GATEWAY_L2_USER_PROMPT_TEMPLATE") if k in persisted
        },
        "placeholders": ["filename", "sheet_names", "headers", "preview"],
    }


@router.put("/l2-prompts")
def l2_prompts_put(req: L2PromptsPutReq):
    """更新 L2 提示词：空串清空（还原默认）；非空即时生效 + 落盘 l2_overrides.yaml。"""
    from src.gateway import small_model as _sm
    # 内存级即时覆盖（含 env 注入，set_prompt_overrides 内部同步）
    _sm.set_prompt_overrides(
        system=req.AI_GATEWAY_L2_SYSTEM_PROMPT if req.AI_GATEWAY_L2_SYSTEM_PROMPT is not None else None,
        template=req.AI_GATEWAY_L2_USER_PROMPT_TEMPLATE if req.AI_GATEWAY_L2_USER_PROMPT_TEMPLATE is not None else None,
    )
    # 持久化：先合再剔。空串=显式还原（删键）；非空=写值；未传=不动。
    merged = _l2ov.load_overrides()
    to_write, to_delete = {}, set()
    for k in ("AI_GATEWAY_L2_SYSTEM_PROMPT", "AI_GATEWAY_L2_USER_PROMPT_TEMPLATE"):
        v = getattr(req, k)
        if v is None:
            continue
        if v == "":
            to_delete.add(k)
        else:
            to_write[k] = v
    _l2ov.save_overrides(to_write, delete=to_delete)
    return {
        "ok": True,
        "system_prompt": _sm.get_system_prompt(),
        "user_prompt_template": _sm.get_user_prompt_template(),
        "persisted": _l2ov.load_overrides(),
    }


@router.get("/l2-shadow-health")
def l2_shadow_health(url: str = ""):
    """影子端点健康检查：GET {base}/health + POST /classify 小探针（不耗 token，非生成调用）。

    url 传参 = 检查表单草稿（未保存）；空 = 检查服务端已生效值。
    内网直连（trust_env=False，不过代理），与 small_model 影子调用同路径。
    """
    import time as _t

    import httpx

    target = (url or os.getenv("AI_GATEWAY_L2_SHADOW_URL") or "").strip()[:512]
    if not target:
        return {"ok": False, "error": "未配置影子端点（AI_GATEWAY_L2_SHADOW_URL 为空）"}
    base = target[:-len("/classify")] if target.endswith("/classify") else target.rstrip("/")
    out: dict = {"ok": False, "url": target}
    t0 = _t.monotonic()
    try:
        hr = httpx.get(f"{base}/health", timeout=5.0, trust_env=False)
        out["health"] = {"status": hr.status_code,
                         "latency_ms": round((_t.monotonic() - t0) * 1000),
                         "body": hr.text[:200]}
        if hr.status_code != 200:
            out["error"] = f"/health HTTP {hr.status_code}"
            return out
    except Exception as e:
        out["health"] = {"latency_ms": round((_t.monotonic() - t0) * 1000)}
        out["error"] = f"/health 连不通：{e!r}"[:200]
        return out
    t0 = _t.monotonic()
    try:
        cr = httpx.post(target, timeout=15.0, trust_env=False, json={
            "text": "hello world, today is sunny",
            "filename": "", "headers": [], "sheet_names": [],
        })
        out["classify"] = {"status": cr.status_code,
                           "latency_ms": round((_t.monotonic() - t0) * 1000)}
        if cr.status_code != 200:
            out["error"] = f"/classify HTTP {cr.status_code}：{cr.text[:200]}"
            return out
        data = cr.json()
        out["classify"].update({k: data.get(k) for k in ("label", "confidence")})
        out["ok"] = data.get("label") in ("CONFIDENTIAL", "NORMAL")
        if not out["ok"]:
            out["error"] = f"/classify 返回异常：{str(data)[:200]}"
    except Exception as e:
        out["classify"] = {"latency_ms": round((_t.monotonic() - t0) * 1000)}
        out["error"] = f"/classify 失败：{e!r}"[:200]
    return out


@router.get("/l2-shadow-stats")
def l2_shadow_stats(hours: int = 168):
    """影子（laya）汇总：扫审计热缓存（Redis 近 5000 条），按窗口聚合主/影判定对照。

    口径：
    - l2_total：窗口内带 l2 判定的条数（只有进 L2 的灰区流量才有；L1 直判无 l2）。
    - shadowed：l2 含 shadow_label 的条数；shadow_degraded：影子失败只丢影子。
    - 一致率 = 主影 label 相同 / 可比条数（主判定 degraded 的不计入可比，兜底 label 不可比）。
    - cells：主密影密(cc)/主密影放(cn)/主放影密(nc)/主放影放(nn)。
    """
    import datetime as _dt

    from src.gateway.audit_store import get_audit_store

    try:
        hours = max(1, min(int(hours), 720))
    except (TypeError, ValueError):
        hours = 168
    cutoff = _dt.datetime.utcnow() - _dt.timedelta(hours=hours)

    def _ts(e) -> object:
        try:
            return _dt.datetime.strptime(
                str(e.get("time") or e.get("ts") or "").strip().replace("T", " ")[:19],
                "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    try:
        entries = get_audit_store().tail(5000) or []
    except Exception:
        entries = []
    scanned = len(entries)
    window = [e for e in entries if isinstance(e, dict) and ((_ts(e) or cutoff) >= cutoff)]

    out: dict = {"hours": hours, "scanned": scanned, "l2_total": 0, "shadowed": 0,
                 "compared": 0, "agree": 0, "agree_rate": None,
                 "shadow_degraded": 0, "main_degraded": 0,
                 "cells": {"cc": 0, "cn": 0, "nc": 0, "nn": 0},
                 "avg_main_latency_ms": None}
    lat_sum, lat_n = 0, 0
    for e in window:
        l2 = e.get("l2")
        if not isinstance(l2, dict):
            continue
        out["l2_total"] += 1
        sl = l2.get("shadow_label")
        if sl not in ("CONFIDENTIAL", "NORMAL"):
            if l2.get("shadow_degraded"):
                out["shadow_degraded"] += 1
            continue
        out["shadowed"] += 1
        try:
            lat = float(l2.get("latency_ms"))
            lat_sum += lat
            lat_n += 1
        except (TypeError, ValueError):
            pass
        ml = l2.get("label")
        if ml == "CONFIDENTIAL" and sl == "CONFIDENTIAL":
            out["cells"]["cc"] += 1
        elif ml == "CONFIDENTIAL":
            out["cells"]["cn"] += 1
        elif sl == "CONFIDENTIAL":
            out["cells"]["nc"] += 1
        else:
            out["cells"]["nn"] += 1
        if l2.get("degraded"):
            out["main_degraded"] += 1
            continue
        out["compared"] += 1
        if ml == sl:
            out["agree"] += 1
    if out["compared"]:
        out["agree_rate"] = round(out["agree"] / out["compared"], 4)
    if lat_n:
        out["avg_main_latency_ms"] = round(lat_sum / lat_n, 1)
    return out


# ---------------- 运维诊断：路由检查器 / provider 健康检查 / 熔断器 ----------------

class RouteInspectFile(BaseModel):
    filename: str = Field(default="upload", max_length=256)
    file_data: str = Field(default="", max_length=12_000_000)  # data-URL 或裸 base64，约 9MB 原字节


class RouteInspectReq(BaseModel):
    model: str = Field(default="", max_length=256)
    text: str = Field(default="", max_length=20000)
    channel: str = Field(default="", max_length=16)  # ""=纯文本；check | chat | responses
    files: List[RouteInspectFile] = Field(default=[])


def _route_inspect_finish(decision, l1_out, l2_out, files_out, channel, model, _rt, provider_keys):
    """route-inspect 收口：resolve 去向 + 统一响应（含 files/channel）。"""
    try:
        prov, _model, notes = _rt.resolve(decision, {"model": model or ""}, None, kind="chat")
        has_key = bool(provider_keys.gateway_key(prov))
        rout = {
            "provider": prov.name,
            "base_url": prov.base_url,
            "local": prov.local,
            "model": _model,
            "notes": notes,
            "gateway_key": {
                "configured": has_key,
                "note": "" if has_key or prov.local
                else "网关侧无该 provider env key：gateway_only 模式外发将 503",
            },
        }
    except Exception as e:
        rout = {"error": repr(e)[:200]}
    return {
        "l1": l1_out,
        "l2": l2_out,
        "final_action": decision.get("action"),
        "final_rule": decision.get("name"),
        "routing": rout,
        "channel": channel,
        "files": files_out,
    }


@router.post("/route-inspect")
async def route_inspect_api(body: RouteInspectReq):
    """路由检查器：对输入文本跑生产同款 L1→L2→路由 决策链，返回完整去向。

    只读诊断：不落审计、不限流、不写 session；L2 真打 :8002（与 /l2/test 同成本）。
    会话粘性/白名单豁免按"无会话、非白名单"的最严口径模拟。

    files（codex ∪ workbuddy 并集）：每项 {filename, file_data?}，file_data 为
    data-URL 或裸 base64。channel 决定复刻哪条真链路：
      check     = /v1/files/check 全量解析（magic 嗅探 + OCR + 表格），无 file_data 时仅文件名；
      chat      = workbuddy image_url 内联图（文件必须带 file_data，由产品内联扫描判定）；
      responses = codex input_file 块（filename 必取；file_data 仅 csv/txt/tsv 解析表头，
                  pdf/xlsx 只看文件名 —— 与线上同宽松口径）。
    无 files 时走纯文本旧逻辑（零改动）。"""
    import base64 as _b64
    import time as _t

    from src.gateway import main as _m
    from src.gateway import inspection as _insp
    from src.gateway import policy as _pol
    from src.gateway import provider_keys
    from src.gateway import routing as _rt

    channel = (body.channel or "").strip().lower()
    files = body.files or []
    if files and channel not in ("check", "chat", "responses"):
        raise HTTPException(422, "带 files 时 channel 须为 check/chat/responses（不带 files 时 channel 忽略）")
    if files and not channel:
        channel = "check"
    if len(files) > 5:
        raise HTTPException(422, "files 最多 5 个")
    if not (body.text or "").strip() and not (body.model or "").strip() and not files:
        raise HTTPException(422, "text 或 model 或 files 至少填一个")
    text = body.text or ""

    def _dec(s: str):
        """data-URL/裸 base64 → bytes；None=没传；b""=解码失败。"""
        s = (s or "").strip()
        if not s:
            return None
        if "," in s:
            head, _, rest = s.partition(",")
            if head.startswith("data:"):
                s = rest
        try:
            return _b64.b64decode(s.strip())
        except Exception:
            return b""

    _MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
             ".pdf": "application/pdf"}

    def _du(fn: str, raw: bytes) -> str:
        ext = ("." + fn.rsplit(".", 1)[-1].lower()) if "." in fn else ""
        return f"data:{_MIME.get(ext, 'application/octet-stream')};base64," + _b64.b64encode(raw).decode()

    files_out: list = []
    file_l2 = None  # check 通道文件式 L2 上下文 {chunks, filename, headers, sheet_names}
    file_ctx = None
    extra_findings: dict = {}

    if channel == "check" and files:
        from starlette.concurrency import run_in_threadpool

        from src.gateway.file_inspector import inspect_file
        parts, ftext, hdrs, sheets, chunks, size = [text] if text.strip() else [], "", [], [], [], 0
        fnames = []
        for fb in files:
            fn = (fb.filename or "upload").strip() or "upload"
            raw = _dec(fb.file_data)
            if raw is None:
                fnames.append(fn)
                parts.append(fn)  # 无字节：仅文件名参与（codex pdf/xlsx 口径）
                files_out.append({"filename": fn, "note": "仅文件名参与决策（无 file_data）"})
                continue
            if raw == b"":
                extra_findings["unparsed_binary"] = True
                fnames.append(fn)
                files_out.append({"filename": fn, "note": "file_data 解码失败，按不可读处理"})
                continue
            info = await run_in_threadpool(inspect_file, raw, fn)
            ft = info.get("text") or ""
            parts.append(ft + " " + fn)
            ftext += ("\n" + ft) if ftext else ft
            fnames.append(fn)
            hdrs += info.get("headers") or []
            sheets += info.get("sheet_names") or []
            chunks += info.get("chunks") or []
            size += len(raw)
            extra_findings.update(info.get("findings") or {})
            files_out.append({"filename": fn, "ext": info.get("ext") or "",
                              "parsed_chars": len(ft)})
        merged = " ".join(p for p in parts if p)
        findings = _insp.inspect_text(merged) if merged else {}
        findings.update(extra_findings)
        ctx = {"text": merged,
               "file": {"ext": "", "filename": " ".join(fnames), "headers": hdrs,
                        "sheet_names": sheets, "text": ftext[:5000],
                        "watermark": "", "size": size},
               "findings": findings, "session": {"confidential": False}}
        ctx["file"]["findings_text"] = str(extra_findings)
        text = merged
        if ftext.strip() or chunks:
            file_l2 = {"chunks": chunks or [merged[:2000]], "filename": " ".join(fnames),
                       "headers": hdrs, "sheet_names": sheets}
    elif channel in ("chat", "responses") and files:
        if channel == "chat" and any(not (f.file_data or "").strip() for f in files):
            raise HTTPException(422, "chat 通道文件必须带 file_data（image_url 内联图形态无裸文件名 wire）")
        if channel == "chat":
            blocks = [{"type": "text", "text": text}] if text.strip() else []
            for f in files:
                raw = _dec(f.file_data)
                if raw:
                    blocks.append({"type": "image_url",
                                   "image_url": {"url": _du(f.filename or "upload", raw)}})
                else:
                    extra_findings["ocr_empty_and_image"] = True  # 解码失败按不可读处理
            inline_text, inline_bad = await _m._inline_media_scan("chat", [{"role": "user", "content": blocks}])
            merged = (text + "\n" + inline_text).strip() if (text.strip() or inline_text.strip()) else ""
            for f in files:
                files_out.append({"filename": f.filename or "upload", "note": "image_url 内联（workbuddy 口径）"})
        else:
            cblocks = ([{"type": "input_text", "text": text}] if text.strip() else [])
            for f in files:
                b = {"type": "input_file", "filename": (f.filename or "upload").strip() or "upload"}
                raw = _dec(f.file_data)
                if raw:
                    low = b["filename"].lower()
                    b["file_data"] = _du(b["filename"], raw)
                    files_out.append({"filename": b["filename"], "note": (
                        "file_data 参与表头解析" if low.endswith((".csv", ".txt", ".tsv"))
                        else "file_data 到达但线上仅解析 csv/txt/tsv（pdf/xlsx 只看文件名）")})
                else:
                    files_out.append({"filename": b["filename"], "note": "仅文件名参与决策"})
                cblocks.append(b)
            rbody = {"input": [{"role": "user", "content": cblocks}]}
            from src.gateway.responses_filectx import extract_responses_filectx
            file_ctx = extract_responses_filectx(rbody, text)
            inline_text, inline_bad = await _m._inline_media_scan("responses", rbody)
            merged = (text + "\n" + inline_text).strip() if (text.strip() or inline_text.strip()) else ""
        if inline_bad:
            extra_findings["ocr_empty_and_image"] = True
        text = merged

    if not files:
        findings = _insp.inspect_text(text) if text else {}
        ctx = {"text": text, "file": {}, "findings": findings,
               "session": {"confidential": False}}
    elif channel in ("chat", "responses"):
        # chat/responses 通道走 _review_text 同款门（含灰区 + 文本式 L2）
        decision0, findings0, l2r0 = await _m._review_text(
            text, None, extra_findings or None, file_ctx=file_ctx)
        l1 = {"name": decision0.get("name"), "action": decision0.get("action"),
              "reason": decision0.get("reason") or ""}
        l1_out = {"rule": l1["name"], "action": l1["action"], "reason": l1["reason"],
                  "findings": {k: v for k, v in (findings0 or {}).items() if v}}
        _gray0 = _m._l2_gray_trigger(findings0)
        l2_out = {"triggered": bool(l2r0),
                  "trigger": "gray" if l2r0 else None,
                  "latency_ms": (l2r0 or {}).get("latency_ms"),
                  "label": (l2r0 or {}).get("label"),
                  "confidence": (l2r0 or {}).get("confidence"),
                  "reason": (l2r0 or {}).get("reason"),
                  "degraded": _m.is_degraded(l2r0) if l2r0 else False}
        if not l2r0:
            l2_out["skipped"] = ("L1 未放行" if l1["action"] != "allow"
                                 else "未进灰区（L2 只看 risk 灰区[20,60)，不计字数）")
        decision = decision0
        return _route_inspect_finish(decision, l1_out, l2_out, files_out, channel or "text",
                                     body, _rt, provider_keys)

    if channel == "check" and files:
        # ctx/findings 已在上分支建成（含文件合并）；文件式 L2（files/check 同款：allow 即审，分块聚合）
        l1 = _pol.decide(_pol.load_policy(), ctx)
        l1_out = {
            "rule": l1.get("name"),
            "action": l1.get("action"),
            "reason": l1.get("reason") or "",
            "findings": {k: v for k, v in (findings or {}).items() if v},
        }
        decision = l1
        l2_out: dict = {"triggered": False}
        if l1.get("action") == "allow" and file_l2:
            t0 = _t.monotonic()
            _ch = file_l2["chunks"] or [text[:2000]]
            if len(_ch) > 1:
                res = await _m.classify_chunks(_ch, filename=file_l2["filename"],
                                               headers=file_l2["headers"],
                                               sheet_names=file_l2["sheet_names"])
            else:
                res = await _m.small_classify(_ch[0][:2000], filename=file_l2["filename"],
                                              headers=file_l2["headers"],
                                              sheet_names=file_l2["sheet_names"])
            l2_out = {
                "triggered": True,
                "trigger": "file",
                "latency_ms": round((_t.monotonic() - t0) * 1000),
                "label": (res or {}).get("label"),
                "confidence": (res or {}).get("confidence"),
                "reason": (res or {}).get("reason"),
                "degraded": _m.is_degraded(res),
            }
            d = _m._l2_decision(res)
            if d is not None:
                decision = d
        elif l1.get("action") == "allow":
            # 全是仅文件名：按合并文本走文本门（文件名关键词可进灰区；不计字数）
            _gray = _m._l2_gray_trigger(findings)
            if _gray:
                t0 = _t.monotonic()
                res = await _m.small_classify(text, filename="", headers=[], sheet_names=[])
                l2_out = {
                    "triggered": True,
                    "trigger": "gray",
                    "latency_ms": round((_t.monotonic() - t0) * 1000),
                    "label": (res or {}).get("label"),
                    "confidence": (res or {}).get("confidence"),
                    "reason": (res or {}).get("reason"),
                    "degraded": _m.is_degraded(res),
                }
                d = _m._l2_decision(res)
                if d is not None:
                    decision = d
            else:
                l2_out["skipped"] = "未进灰区（L2 只看 risk 灰区，不计字数）"
        else:
            l2_out["skipped"] = "L1 未放行"
        return _route_inspect_finish(decision, l1_out, l2_out, files_out, channel,
                                     body.model, _rt, provider_keys)

    # 纯文本旧逻辑（零改动）
    findings = _insp.inspect_text(text) if text else {}
    ctx = {"text": text, "file": {}, "findings": findings,
           "session": {"confidential": False}}
    l1 = _pol.decide(_pol.load_policy(), ctx)
    l1_out = {
        "rule": l1.get("name"),
        "action": l1.get("action"),
        "reason": l1.get("reason") or "",
        "findings": {k: v for k, v in (findings or {}).items() if v},
    }

    decision = l1
    l2_out: dict = {"triggered": False}
    _gray = _m._l2_gray_trigger(findings)
    if l1.get("action") == "allow" and _gray:
        t0 = _t.monotonic()
        res = await _m.small_classify(text, filename="", headers=[], sheet_names=[])
        l2_out = {
            "triggered": True,
            "trigger": "gray",
            "latency_ms": round((_t.monotonic() - t0) * 1000),
            "label": (res or {}).get("label"),
            "confidence": (res or {}).get("confidence"),
            "reason": (res or {}).get("reason"),
            "degraded": _m.is_degraded(res),
        }
        d = _m._l2_decision(res)
        if d is not None:
            decision = d
    else:
        l2_out["skipped"] = ("L1 未放行" if l1.get("action") != "allow"
                             else "未进灰区（L2 只看 risk 灰区，不计字数）")

    return _route_inspect_finish(decision, l1_out, l2_out, [], "text",
                                 body.model, _rt, provider_keys)


class ProviderCheckReq(BaseModel):
    name: str = Field(default="", max_length=128)
    timeout: float = Field(default=10.0, ge=1, le=30)


@router.post("/providers/check")
def providers_check_api(body: ProviderCheckReq):
    """provider 主动健康检查：GET {base_url}/models 测连通+鉴权+延迟。

    name 空 = 查全部；不发起真实模型调用（不耗 token）。"""
    import time as _t

    import httpx

    from src.gateway.providers import load_routing
    from src.gateway.routing import upstream_proxy

    cfg = load_routing()
    names = [body.name] if body.name else list(cfg.providers)
    results = []
    for name in names:
        prov = cfg.get(name)
        entry: dict = {"name": name, "ok": False}
        if prov is None:
            entry["error"] = "未知 provider"
            results.append(entry)
            continue
        base = (prov.base_url or "").rstrip("/")
        if not base:
            entry["error"] = "未配置 base_url"
            results.append(entry)
            continue
        key, src = _models_list_key(prov, "")
        entry["key_source"] = src
        t0 = _t.monotonic()
        try:
            resp = httpx.get(f"{base}/models", headers=prov.headers(key),
                             timeout=body.timeout, trust_env=False,
                             proxy=upstream_proxy())
            entry["latency_ms"] = round((_t.monotonic() - t0) * 1000)
            entry["status"] = resp.status_code
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data") if isinstance(data, dict) else data
                entry["ok"] = True
                entry["model_count"] = len([i for i in (items or []) if i])
            else:
                entry["error"] = f"上游 HTTP {resp.status_code}"
        except Exception as e:
            entry["latency_ms"] = round((_t.monotonic() - t0) * 1000)
            entry["error"] = repr(e)[:200]
        results.append(entry)
    return {"results": results}


@router.get("/circuit")
def circuit_api():
    """熔断器逐 provider 明细（state 为已应用 OPEN→HALF_OPEN 自动迁移的有效态）。"""
    import time as _t

    from src.gateway.circuit_breaker import get_breaker

    b = get_breaker()
    now = _t.time()
    recovery = int(os.getenv("AI_GATEWAY_CB_RECOVERY_SECONDS", "30"))
    from src.gateway.providers import load_routing
    # 全量 provider（含从未跳闸的，默认 closed），便于 UI 直接对任一 provider 手动置位
    snap = b.snapshot()
    names = list(load_routing().providers)
    for n in snap:
        if n not in names:
            names.append(n)
    providers = {}
    for name in names:
        s = snap.get(name) or {"state": "closed", "opened_at": 0.0,
                                       "last_failure": 0.0, "failure_count_window": 0,
                                       "half_open_successes": 0}
        d = dict(s)
        d["state"] = b.state(name)
        d["cooldown_remaining_s"] = (
            round(recovery - (now - s["opened_at"]), 1)
            if s["state"] == "open" and s["opened_at"] else 0.0)
        providers[name] = d
    return {
        "providers": providers,
        "config": {
            "failure_threshold": int(os.getenv("AI_GATEWAY_CB_FAILURE_THRESHOLD", "5")),
            "window_seconds": int(os.getenv("AI_GATEWAY_CB_WINDOW_SECONDS", "60")),
            "recovery_seconds": recovery,
            "success_threshold": int(os.getenv("AI_GATEWAY_CB_SUCCESS_THRESHOLD", "2")),
        },
    }


class CircuitSetReq(BaseModel):
    state: str = Field(pattern="^(open|closed)$")


@router.post("/circuit/{name}")
def circuit_set_api(request: Request, name: str, body: CircuitSetReq):
    """手动置位：open=立即熔断（进入恢复倒计时）；closed=复位该 provider。带留痕。"""
    from src.gateway.circuit_breaker import get_breaker

    try:
        snap = get_breaker().set_state(name, body.state)
    except ValueError as e:
        raise HTTPException(422, str(e))
    get_admin_store().log_ops("circuit_set", f"{name} -> {body.state}", _operator(request))
    return {"ok": True, "provider": name, "state": body.state, "snapshot": snap}
