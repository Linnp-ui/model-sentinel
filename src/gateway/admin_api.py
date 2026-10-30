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
  GET    /admin/api/policies                 5.1 L1 规则列表
  POST   /admin/api/policies                 新增规则
  PUT    /admin/api/policies/{name}          更新规则（可重命名：body 里带 new_name）
  DELETE /admin/api/policies/{name}
  POST   /admin/api/policies/test            L1 规则测试（text/filename/size/model/client_key/hour -> 命中详情）
  GET    /admin/api/policies/match-choices  5.2 构造器可选项（前端下拉数据源）
  POST   /admin/api/policies/build-when      5.2 表单语义 -> when 条件（前端预览用）
  POST   /admin/api/policies/build-when-group 条件组（多行 any/all）-> when 条件
  POST   /admin/api/policies/parse-when      when 条件 -> 构造器控件值（编辑预填，单条件或条件组）
  POST   /admin/api/l2/generate-rule         5.2 匹配项 AI 生成（只建议不落库）
  POST   /admin/api/l2/test                  5.3 L2 规则测试（待识别内容 -> 审查判定）

M5（别名候选选择）：
  GET    /admin/api/providers                provider 注册表（脱敏，不含密钥）
  GET    /admin/api/providers/{name}/models  代理拉取供应商 /models（管理员选模型用）

M6（运维留痕）：

状态条：
  GET    /admin/api/status/stream            SSE，每 60s 推 health/circuit/l2/audit 聚合
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import re
import signal
import threading
import time
from typing import Dict, List, Optional, Union

import json

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.gateway.admin_store import AlreadyRegistered, get_admin_store, mask_key
from src.gateway.stat_scope import to_beijing
from src.gateway import l2_overrides as _l2ov
from src.gateway import policy_admin

router = APIRouter(prefix="/admin/api")


# ---------------- helpers ----------------

_VERR_MSG_ZH = {
    "missing": "缺少必填项",
    "string_too_long": "内容过长",
    "string_too_short": "内容过短",
    "string_pattern_mismatch": "格式不匹配",
    "string_type": "需要文本",
    "int_parsing": "需要整数",
    "bool_parsing": "需要布尔值",
    "dict_type": "需要对象",
    "list_type": "需要列表",
    "greater_than": "数值过小",
    "less_than": "数值过大",
}


def _verrors(e: ValidationError) -> list:
    """e.errors() 里 value_error 的 ctx 持有 ValueError 实例（非 JSON 可序列化，
    形如 {'error': ValueError(...)}），直接当 422 detail 会让 FastAPI 序列化
    炸成 500——递归把异常压成 str。同时把 Pydantic 自带英文 msg 换成中文。"""
    out = []
    for err in e.errors():
        err.pop("input", None)  # 不回显原始值（密码/长文本），前端只用 loc/msg
        err.pop("url", None)    # RequestValidationError.errors() 不接受 include_url 参数
        ctx = err.get("ctx")
        if isinstance(ctx, BaseException):
            ctx = str(ctx)
        elif isinstance(ctx, dict):
            ctx = {k: (str(v) if isinstance(v, BaseException) else v) for k, v in ctx.items()}
        if ctx is not None and not isinstance(ctx, (str, int, float, bool, list, dict)):
            ctx = str(ctx)
        if ctx is not err.get("ctx"):
            err = {**err, "ctx": ctx}
        msg = str(err.get("msg", ""))
        if err.get("type") == "value_error":
            msg = msg.removeprefix("Value error, ")
        else:
            msg = _VERR_MSG_ZH.get(err.get("type", ""), msg)
        if msg != err.get("msg"):
            err = {**err, "msg": msg}
        out.append(err)
    return out


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
    weekly_cost_cap: Optional[int] = Field(default=None, ge=0)


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
        raise HTTPException(403, "原密码不正确")
    if body.old_password == body.new_password:
        raise HTTPException(422, "新密码不能与原密码相同")
    try:
        st.set_admin_password(body.new_password)
    except ValueError as e:
        raise HTTPException(422, str(e))
    # session 密钥随哈希轮换，旧 cookie 全部失效；客户端需重新登录
    return {"ok": True, "relogin": True, "backend": st.backend}


# ---------------- 4.2 API KEY ----------------

@router.get("/keys")
def list_keys():
    items = get_admin_store().list_api_keys()
    for r in items:
        r["created_at"] = to_beijing(r.get("created_at"))
    return {"items": items}


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
    """批量导入（手写 key）：每行一条，`名称,key` 逗号分隔（全角逗号兼容）。

    只写 key 不写名称也收 —— 名称自动取 `导入-<行号>`（统计归因用；手头只有 key 时用）。
    `#` 起头为注释。逐行返回结果，响应不含完整 key（只回掩码）。
    """
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
            name, key = "", line  # 只给了 key：名称按行号自动生成
        key = key.strip()
        name = name.strip()
        if name.startswith("sk-") or name.startswith("pk_"):
            results.append({"line": idx, "key_masked": "", "status": "invalid", "message": "第 1 段疑似 KEY，格式应为 名称,key"})
            continue
        if len(key) < 8:
            results.append({"line": idx, "key_masked": (key if "*" in key else mask_key(key)), "status": "invalid", "message": "key 至少 8 位"})
            continue
        if not name:
            name = f"导入-{idx:02d}"
        if len(name) > 128:
            results.append({"line": idx, "key_masked": (key if "*" in key else mask_key(key)), "status": "invalid", "message": "名称过长"})
            continue
        try:
            get_admin_store().create_api_key(key, name)
            results.append({"line": idx, "key_masked": (key if "*" in key else mask_key(key)), "status": "ok", "message": name})
            added += 1
        except ValueError as e:
            msg = str(e)
            status = "duplicate" if isinstance(e, AlreadyRegistered) else "invalid"
            results.append({"line": idx, "key_masked": (key if "*" in key else mask_key(key)), "status": status, "message": msg})
    if not results:
        raise HTTPException(422, "没有可导入的行")
    return {"ok": True, "added": added, "results": results}


@router.patch("/keys/{kid}")
def update_key(kid: int, body: ApiKeyUpdateReq):
    try:
        get_admin_store().update_api_key(kid, name=body.name, owner=body.owner, note=body.note, weekly_cost_cap=body.weekly_cost_cap)
    except KeyError:
        raise HTTPException(404, "key 不存在")
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
        raise HTTPException(422, "kind 只能是 black 或 white")
    items = get_admin_store().list_key_rules(kind)
    for it in items:
        it["key_masked"] = mask_key(it.pop("key_value", ""))
        it["created_at"] = to_beijing(it.get("created_at"))
    return {"items": items}


@router.post("/keyrules")
def create_keyrule(body: KeyRuleCreateReq):
    kv = (body.key_value or "").strip()
    if not kv and body.key_name.strip():
        # 按登记名解析明文（管理页下拉用；明文不出后端）
        kv = {k["name"]: k["key_plain"] for k in get_admin_store().list_api_keys_raw()}.get(body.key_name.strip(), "")
        if not kv:
            raise HTTPException(422, "该名称未登记（请先登记 key）")
    if not (8 <= len(kv) <= 256):
        raise HTTPException(422, "key 长度需在 8-256 字符之间")
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


@router.get("/stats/billing")
def stats_billing_api(hours: float = 168.0):
    """Token 计费：KEY × 实际模型 × 单价 → ¥（内部成本分摊口径，不等于上游账单）。"""
    store = get_admin_store()
    return _stats.build_billing(store, hours)


@router.get("/stats/chart")
def stats_chart_api(hours: float = 24.0):
    """总览折线图（QPS+延迟分位；request_log 源按窗口重分桶，点数与环线相当）。"""
    store = get_admin_store()
    since, until = _win(hours)
    return store.stats_chart_series(since, until)




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
    cutoff = _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime(_t.time() - days * 86400))
    return {"deleted": get_admin_store().purge_request_logs(cutoff)}


@router.get("/suggestions")
def suggestions_api(hours: float = 24.0):
    """3.3 策略调节建议（规则引擎）。

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
            raise HTTPException(422, "需填 limit_model 和正数 limit_rpm")
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
            raise HTTPException(422, "rule_name 不能为空")
        from src.gateway import policy_admin as _pa
        try:
            deleted = _pa.delete_policy(req.rule_name)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if not deleted:
            raise HTTPException(404, "规则不存在")
        return {"ok": True, "removed": req.rule_name}
    if req.kind == "reorder":
        from src.gateway import policy_admin as _pa
        if req.order:
            cur = {p["name"]: p for p in _pa.list_policies()}
            names = [n for n in dict.fromkeys(req.order) if n and n in cur]
            # 优先级被锁定的规则不参与重排：从名单中剔除，避免覆盖其 priority
            locked = [n for n in names if cur[n].get("priority_locked") or cur[n].get("locked")
                      or cur[n].get("locked_priority") or cur[n].get("pinned")
                      or cur[n].get("fixed_priority")]
            names = [n for n in names if n not in locked]
            if len(names) < 2:
                raise HTTPException(422, "可重排规则不足 2 条（锁定规则不可重排）")
            missing = [n for n in req.order if n and n not in cur]
            if missing:
                raise HTTPException(404, f"规则不存在：{missing[0]}")
            # 按锁定规则分段：每段非锁定规则只在段内 permute，不跨锁定屏障
            done = {}
            locked_fields = ("priority_locked", "locked", "locked_priority", "pinned", "fixed_priority")
            def _is_locked(p):
                return any(p.get(k) for k in locked_fields)
            # 当前策略按 priority 排序
            ordered = sorted(cur.values(), key=lambda x: x.get("priority", 100))
            # 切段
            segments = []
            run = []
            for p in ordered:
                if _is_locked(p):
                    if run:
                        segments.append(run)
                        run = []
                else:
                    run.append(p)
            if run:
                segments.append(run)
            for seg in segments:
                seg_names_set = {p.get("name") for p in seg}
                seg_selected = [n for n in names if n in seg_names_set]
                if len(seg_selected) < 2:
                    continue
                slots = sorted(cur[n]["priority"] for n in seg_selected)
                for n, sl in zip(seg_selected, slots):
                    row = dict(cur[n])
                    row["priority"] = sl
                    _pa.upsert_policy(row, original_name=n)
                    done[n] = sl
            return {"ok": True, "reordered": done, "skipped_locked": locked}
        if not req.rule_a or not req.rule_b or req.rule_a == req.rule_b:
            raise HTTPException(422, "需填 rule_a 和 rule_b（且不同）")
        cur = {p["name"]: p for p in _pa.list_policies()}
        if req.rule_a not in cur or req.rule_b not in cur:
            raise HTTPException(404, "规则不存在")
        ra, rb = dict(cur[req.rule_a]), dict(cur[req.rule_b])
        ra["priority"], rb["priority"] = rb["priority"], ra["priority"]
        _pa.upsert_policy(ra, original_name=req.rule_a)
        _pa.upsert_policy(rb, original_name=req.rule_b)
        return {"ok": True, "reordered": {req.rule_a: ra["priority"],
                                          req.rule_b: rb["priority"]}}
    if req.kind == "unwhite":
        if req.rule_id is None:
            raise HTTPException(422, "rule_id 不能为空")
        rows = [r for r in store.list_key_rules() if r["id"] == req.rule_id]
        if not rows or rows[0]["kind"] != "white":
            raise HTTPException(404, "白名单规则不存在")
        if rows[0].get("source") not in ("strategy", "auto"):
            raise HTTPException(409, "手工添加的规则请手动删除")
        store.remove_key_rule(req.rule_id)
        return {"ok": True, "removed": req.rule_id}
    kv = req.key
    if not kv and req.key_name:
        kv = {k["name"]: k["key_plain"] for k in store.list_api_keys_raw()}.get(req.key_name, "")
    if not kv:
        raise HTTPException(422, "key 或名称解析不到（请检查登记）")
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
        raise HTTPException(422, _verrors(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "policy": pol.model_dump()}


@router.get("/model-policy/spend")
def model_spend_api(hours: float = 168.0):
    """每模型实际花费（计费口径），供模型策略页「推荐等级」：配置单价 × 实际用量混合排序。

    复用 build_billing 定价口径（内网 cost=0、价目表未命中 priced=false 不计）；
    计费行是 KEY×模型 粒度，这里按 provider/model 求和。
    rate_per_1m = 实际花费/实际 token = 含真实 in/out/cached 混合的有效费率。
    """
    from src.gateway.admin_store import get_admin_store
    from src.gateway import stats_service
    hours = min(max(hours, 1.0), 720.0)
    d = stats_service.build_billing(get_admin_store(), window_hours=hours)
    per: Dict[str, Dict[str, float]] = {}
    for it in d.get("items", []):
        if not it.get("priced"):
            continue
        m = str(it.get("model") or "")
        if not m:
            continue
        e = per.setdefault(m, {"cost": 0.0, "prompt": 0, "completion": 0})
        e["cost"] += float(it.get("cost") or 0)
        e["prompt"] += int(it.get("prompt") or 0)
        e["completion"] += int(it.get("completion") or 0)
    models = {}
    for m, e in per.items():
        toks = e["prompt"] + e["completion"]
        models[m] = {
            "cost": round(e["cost"], 2),
            "tokens": toks,
            "rate_per_1m": round(e["cost"] / toks * 1e6, 4) if toks else 0.0,
        }
    return {"hours": d.get("hours"), "models": models}



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
    # 与 main._review_text ctx 的 model / client.key / time.hour 对齐（干跑可匹配）
    model: str = ""
    client_key: str = ""
    hour: Optional[int] = Field(default=None, ge=0, le=23)


class ParseWhenReq(BaseModel):
    when: object


class BuildWhenReq(BaseModel):
    # 白名单从 policy_admin 的映射表派生，不另抄一份：新增匹配类型只改那一处
    # （size / hour_range 是自带字段的特例，不在映射表里）
    content_type: str = Field(pattern="^(" + "|".join(sorted(policy_admin.CONTENT_TYPE_FIELD) + ["size", "hour_range", "pii_score"]) + ")$")
    match_type: str = Field(pattern="^(" + "|".join(sorted(policy_admin.MATCH_TYPE_OP) + ["size", "hour_range", "pii_score"]) + ")$")
    match_value: object


class BuildWhenGroupReq(BaseModel):
    combiner: str = Field(pattern="^(any|all)$")
    rows: list[dict] = Field(min_length=1, max_length=8)


class L2GenerateReq(BaseModel):
    description: str = Field(min_length=2, max_length=500)
    # 「换一组」：已生成的匹配项，非空时提示模型避开这些词换一组（只传关键词类）
    exclude: list = Field(default_factory=list, max_length=50)
    # 覆盖范围：允许生成的 content_type 集合（prompt/filename/file/sheet_names/headers/…），
    # 空=不限；越界输出整组拒
    scopes: list = Field(default_factory=list, max_length=12)


class L2TestReq(BaseModel):
    text: str = Field(default="", max_length=20000)
    filename: str = ""
    headers: list = Field(default_factory=list)
    sheet_names: list = Field(default_factory=list)


# ---------- 对外模型别名（Auto-AB 等动态路由） ----------

class AliasMemberReq(BaseModel):
    provider: str
    model: str
    priority: int = 100
    enabled: bool = True
    tier: int = 2  # 1 轻 / 2 中 / 3 重（复杂度路由的档位）


class AliasGroupSaveReq(BaseModel):
    description: str = ""
    members: list[AliasMemberReq]
    new_name: str = ""  # 非空且 ≠ URL 组名 → 整组重命名（撞名 422）
    intent: bool = False  # 兼容字段：复杂度路由已定死常开，此值不影响行为


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
    from .user_models import find_provider, get_store
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
                "intent": True,  # 复杂度路由定死常开（2026-10-26 起，不再按组开关）
                "candidates": [f"{m['provider']}/{m['model']}" for m in members],
            },
        })
    try:
        from .routing import load_routing
        _rcfg = load_routing()
        for im in load_model_policy().internal_models:
            if not im.enabled or im.model in seen:
                continue
            # 只暴露「能对话的内网模型」：provider 须在 routing.yaml 里且是 chat。
            # laya 是 /classify 决策模型（非 chat 协议，model_policy 注释即写「不暴露」），
            # 暴露出去只会让客户端拿它当对话模型调 → 落回外网上游。
            _cand = _rcfg.get(im.provider) or find_provider(im.provider)
            if _cand is None or _cand.kind != "chat":
                continue
            seen.add(im.model)
            data.append({
                "id": im.model,
                "object": "model",
                "created": now,
                "owned_by": "gateway",
                "gateway": {
                    "kind": "chat",
                    "local": bool(_cand.local),
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
    # 对外只展示白名单里的模型（AI_GATEWAY_PUBLIC_MODELS，逗号分隔；空 = 不过滤，旧行为）。
    # 只管「可发现性」，不管「可路由性」：没列出的名字老客户端照常能用 —— 收窄列表
    # 不该等于打断在用的链路（未知模型名走原样交上游/内网登记模型，见 routing.resolve）。
    _allow = {s.strip() for s in (os.getenv("AI_GATEWAY_PUBLIC_MODELS") or "").split(",") if s.strip()}
    if _allow:
        data = [d for d in data if d["id"] in _allow]
        if not data:
            # 白名单一个都没匹配到 = 名字写错/模型已改名 ⇒ /v1/models 会静默返回空列表，必须留痕
            logging.getLogger("gateway").warning(
                "public_models: AI_GATEWAY_PUBLIC_MODELS=%s 匹配不到任何模型，/v1/models 返回空列表",
                ",".join(sorted(_allow)))
    return data


@router.get("/models/public")
def public_models_api():
    """与 /v1/models 同一构造（build_public_models），供管理台对齐展示。"""
    return {"data": build_public_models()}


def _same_provider(a: str | None, b: str | None) -> bool:
    """provider 名比较：大小写不敏感。

    别名成员 / 策略规则 target 里存的名字可能是展示名（Deepseek），而 routing.yaml 的
    键是小写（deepseek）——改名级联与删除保护都靠这个比较，大小写敏感就会漏掉引用。
    """
    x = str(a or "").strip().lower()
    y = str(b or "").strip().lower()
    return bool(x) and x == y


def _normalize_alias_providers(members: list) -> list[dict]:
    """把别名成员的 provider 归一成 routing.yaml 的规范键，并挡住解析不到的名字。

    2026-10-21：别名组曾存 'Deepseek'/'Vllm_Local' 展示名，而 routing.yaml 的键是小写、
    查找又大小写敏感 ⇒ 走别名路径必然 503（unknown provider）。现在：解析得到就归一成规范键
    （库里从此只存规范名 ⇒ 改名级联/删除保护的精确匹配永远命中）；解析不到直接 422，
    并列出可用 provider，不让它进库变运行期 503。
    """
    from .providers import load_routing
    from .user_models import find_provider
    cfg = load_routing()
    out: list[dict] = []
    bad: list[str] = []
    for m in members:
        item = m.model_dump() if hasattr(m, "model_dump") else dict(m)
        pr = str(item.get("provider") or "").strip()
        if not pr:
            out.append(item)
            continue
        canon = None
        if pr in cfg.providers:
            canon = pr
        else:
            for k in cfg.providers:
                if k.lower() == pr.lower():
                    canon = k
                    break
        if canon is None and find_provider(pr) is not None:
            canon = pr          # 用户自注册模型：名字自定，不归一
        if canon is None:
            if pr not in bad:
                bad.append(pr)
            out.append(item)
            continue
        item["provider"] = canon
        out.append(item)
    if bad:
        raise ValueError(
            f"provider 不存在：{'、'.join(bad)}；可用：{'、'.join(sorted(cfg.providers))}")
    return out


@router.put("/aliases/{name}")
def update_alias_group_api(name: str, body: AliasGroupSaveReq):
    """整组保存别名成员（按 priority 升序重排为 10,20,...；最小者为默认）。

    body.new_name 非空且 ≠ URL 组名 → 整组重命名（目标名撞名 422），返回的
    group 为最终名。

    复杂度路由定死常开（2026-10-26 起不再提供开关）：intent 字段仅保留兼容，
    保存值不再影响行为——选头只看启用成员档位（tier）是否有 ≥2 种。
    """
    try:
        members = _normalize_alias_providers(body.members)
        saved = get_admin_store().update_alias_group(
            name,
            members,
            description=body.description,
            new_name=body.new_name,
            intent=body.intent,
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
    except KeyError:
        raise HTTPException(404, f"别名组不存在：{name}")
    except ValueError as e:
        raise HTTPException(422, str(e))
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
            "timeout": p.timeout,
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


class OnBlockModeReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = Field(pattern="^(fallback_local|reject)$")


@router.get("/routing")
def get_routing_api():
    """routing.yaml 的 routing 段（on_block 等），供管理台显示/切换拦截模式。"""
    from src.gateway import providers as _pv
    raw = _pv.load_raw()
    r = raw.get("routing") if isinstance(raw.get("routing"), dict) else {}
    return {"routing": {
        "default_external": r.get("default_external"),
        "default_local": r.get("default_local"),
        "default_embedding": r.get("default_embedding"),
        "on_block": r.get("on_block", "fallback_local"),
        "on_missing_key": r.get("on_missing_key"),
    }}


@router.put("/routing/on-block")
def set_on_block_api(body: OnBlockModeReq):
    """切换 on_block：fallback_local（block 降本地，UX 好）↔ reject（block 真 403）。
    原子写 routing.yaml + mtime 热重载；无版本快照表（routing 无 admin_store 版本），靠 git 回滚。"""
    from src.gateway import providers as _pv
    import yaml as _y
    raw = _pv.load_raw()
    raw.setdefault("routing", {})["on_block"] = body.mode
    p = _pv._routing_path()
    text = _y.safe_dump(raw, allow_unicode=True, sort_keys=False)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    _pv.invalidate_cache()
    return {"ok": True, "mode": body.mode}


class ProviderUpsertReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=4, max_length=512)
    api_key_env: str = Field(default="", max_length=128)
    api_mode: str = Field(default="openai")
    kind: str = Field(default="chat", pattern="^(chat|embedding)$")
    default_model: str = Field(default="", max_length=256)
    timeout: float = Field(default=60.0, ge=1, le=600)
    local: bool = False
    extra_headers: Optional[Dict[str, str]] = None  # None=未传，更新时不动原值


class ProviderCreateReq(ProviderUpsertReq):
    name: str = Field(min_length=1, max_length=64, pattern="^[A-Za-z0-9_-]+$")


def _provider_payload(body: ProviderUpsertReq) -> Dict[str, Any]:
    d = body.model_dump(exclude_none=True)
    d.pop("name", None)
    d.pop("new_name", None)
    d.pop("api_key", None)
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
        if _same_provider(val, name):
            refs.append(f"{field_}")
    pol = _mp.load_model_policy()
    for c in pol.external_candidates:
        if _same_provider(c.provider, name):
            refs.append(f"外部候选 {name}/{c.model}")
    for c in pol.internal_models:
        if _same_provider(c.provider, name):
            refs.append(f"内部模型 {name}/{c.model}")
    for g in get_admin_store().list_alias_groups():
        if any(_same_provider(m.get("provider"), name) for m in g.get("members", [])):
            refs.append(f"别名组 {g.get('name')}")
    for rule in _pa.list_policies():
        if _same_provider((rule.get("target") or {}).get("provider"), name):
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
    target = name  # provider 名创建后不可改（身份键，被 routing/model_policy/别名组/policy 引用）
    # move provider entry
    if name in provs:
        entry = provs.pop(name)
        provs[target] = entry
    old_local = (provs.get(name) or {}).get("local")
    merged = {k: v for k, v in (provs.get(target) or {}).items() if k != "api_key_value"}
    payload = _provider_payload(body)
    payload.pop("new_name", None)
    merged.update(payload)
    # local 是安全边界：创建后不可改（误设=内容静默出境），强制保留原值
    if old_local is not None:
        merged["local"] = bool(old_local)
    provs[target] = merged
    raw["providers"] = provs
    # providers.save_providers  expects full dict and writes yaml; but raw already modified routing; save_providers reloads raw anyway losing refs? Use save_providers only for providers + routing? save_providers writes whole raw including routing we mutated? Let's inspect: save_providers reads _routing_path raw independently, doesn't accept routing. It overwrites providers only but preserves raw routing? It calls load_raw fresh, so our raw mutations not persisted. Need instead write routing.yaml manually.
    from src.gateway import providers as _pv
    # persist routing + providers via yaml
    try:
        import yaml as _y
        pth = _pv._routing_path()
        _y.safe_dump(raw, open(pth, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
        _pv.invalidate_cache()
    except Exception as e:
        raise HTTPException(422, f"routing 保存失败：{e}")
    get_admin_store().log_ops("provider_rename" if target != name else "provider_upsert", f"{name} -> {target}", _operator(request))
    return {"ok": True, "name": target}


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
def create_policy_api(body: PolicyRuleReq, request: Request):
    from src.gateway import policy_admin
    try:
        rule = policy_admin.upsert_policy(body.model_dump(), operator=_operator(request))
    except ValidationError as e:
        raise HTTPException(422, _verrors(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "rule": rule}


@router.put("/policies/{name}")
def update_policy_api(name: str, body: PolicyRuleReq, request: Request):
    from src.gateway import policy_admin
    data = body.model_dump()
    new_name = str((data.pop("new_name", None) or "") or name)
    data["name"] = new_name
    try:
        rule = policy_admin.upsert_policy(data, original_name=name, operator=_operator(request))
    except ValidationError as e:
        raise HTTPException(422, _verrors(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "rule": rule}


@router.delete("/policies/{name}")
def delete_policy_api(name: str, request: Request):
    from src.gateway import policy_admin
    try:
        deleted = policy_admin.delete_policy(name, operator=_operator(request))
    except ValueError as e:
        raise HTTPException(422, str(e))
    if not deleted:
        raise HTTPException(404, "规则不存在")
    return {"ok": True, "deleted": name}


@router.get("/policy/versions")
def policy_versions_api(limit: int = 50):
    """policy.yaml 历史版本列表（每次写盘前快照）。

    每行带 `changes` = 该快照与「下一更新状态」的内容差（最新版与现网文件比，
    含手改漂移）；yaml_text 仍只给 200 字预览，全文走 /versions/{vid}。"""
    from src.gateway import policy_admin
    items = get_admin_store().list_policy_versions(limit, full=True)  # 新→旧
    try:
        current = policy_admin._policy_path().read_text(encoding="utf-8")
    except OSError:
        current = ""
    # 先算完所有 diff 再截断：截断会毁掉后面行要引用的相邻全文
    for i, r in enumerate(items):
        r["changes"] = policy_admin.diff_policy_texts(
            r["yaml_text"], items[i - 1]["yaml_text"] if i > 0 else current)
    for r in items:
        r["yaml_text"] = (r["yaml_text"] or "")[:200]
        r["ts"] = to_beijing(r.get("ts"))
    return {"items": items}


@router.get("/policy/versions/{vid}")
def policy_version_api(vid: int):
    v = get_admin_store().get_policy_version(vid)
    if v is None:
        raise HTTPException(404, "版本不存在")
    return v


@router.post("/policy/versions/{vid}/rollback")
def policy_version_rollback_api(vid: int, request: Request):
    """回滚到指定版本（走 Pydantic 校验 + 原子落盘 + 现状再快照，可再反悔）。"""
    from src.gateway import policy_admin
    v = get_admin_store().get_policy_version(vid)
    if v is None:
        raise HTTPException(404, "版本不存在")
    op = _operator(request)
    try:
        n = policy_admin.restore_policy_yaml(v["yaml_text"], operator=op, source="rollback")
    except (ValueError, ValidationError) as e:
        raise HTTPException(422, str(e))
    get_admin_store().log_ops("policy_rollback", f"vid={vid} rules={n}", op)
    return {"ok": True, "rules": n}


@router.post("/policies/test")
def test_policy_api(body: PolicyTestReq):
    return policy_admin.test_rule(body.text, body.filename, body.size,
                                  body.model, body.client_key, body.hour)


@router.get("/policies/match-choices")
def match_choices_api():
    """5.2 构造器可选项（前端下拉的数据源）。

    与 build-when 共用 policy_admin.MATCH_CHOICES 一张表，不在前端另抄一份：
    后端加了匹配类型而前端没跟上，表现为下拉里选不到，不是静默错配。"""
    from src.gateway import policy_admin
    return {
        "choices": [
            {"key": f"{ct}|{mt}", "content_type": ct, "match_type": mt, "label": label, "desc": desc}
            for ct, mt, label, desc in policy_admin.MATCH_CHOICES
        ],
        # 自带字段的匹配类型不绑内容类型，单列出来让 UI 也能选（本地时段 hour_range
        # 在下拉里可选，但不能进多行条件组——它自身就是 any/all 壳，build_when_group 显式拒）
         "self_field": [
            {"key": f"size|size", "content_type": "size", "match_type": "size", "label": "文件大小 ≥",
             "desc": "匹配项=字节数：文件大小达到或超过该值才算命中（例：1048576=1MB，20971520=20MB）。只对带附件的请求生效，纯文本请求 size=0 永不命中"},
            {"key": f"hour_range|hour_range", "content_type": "hour_range", "match_type": "hour_range", "label": "本地时段",
             "desc": "匹配项=“起-止”小时（0-23，例：22-6 跨午夜、9-18 同日）：请求时本地小时落在区间内就算命中。纯文本与文件请求都生效"},
            {"key": f"pii_score|pii_score", "content_type": "pii_score", "match_type": "pii_score", "label": "加权隐私分 ≥",
             "desc": "匹配项=分数阈值（整数，例：60）：加权隐私 PII 评分达到或超过该值才算命中。身份证 70 分、银行卡 Luhn 60 分、密钥类 90+ 分"},
        ],
    }


@router.post("/policies/parse-when")
def parse_when_api(body: ParseWhenReq):
    """build-when 的逆：把已有规则的 when 还原成构造器控件值，供编辑时预填。

    认不出的形状返回 parsed=null——前端保持构造器空白，不拿默认值覆盖
    用户已写好的 when（多条件 / 无构造器等价物的手写 when 走 JSON 编辑）。"""
    from src.gateway import policy_admin
    return {"parsed": policy_admin.parse_when(body.when)}


@router.post("/policies/build-when")
def build_when_api(body: BuildWhenReq):
    from src.gateway import policy_admin
    try:
        when = policy_admin.build_when(body.content_type, body.match_type, body.match_value)
    except (ValueError, re.error) as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "when": when}


@router.post("/policies/build-when-group")
def build_when_group_api(body: BuildWhenGroupReq):
    """条件组（多行 字段+匹配方式+匹配项，any/all 组合）-> when。与 build-when 共用
    policy_admin 的映射表与逐行校验，前端构造器多条件预览用。"""
    from src.gateway import policy_admin
    try:
        when = policy_admin.build_when_group(body.combiner, body.rows)
    except (ValueError, re.error) as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "when": when}


# ---------------- M2: 5.2/5.3 L2 生成与测试 ----------------

@router.post("/l2/generate-rule")
async def l2_generate_rule_api(body: L2GenerateReq):
    """AI 生成匹配项建议（5.2）。只返回建议，不落库；失败由 UI 手动填写兜底。"""
    from src.gateway.rule_ai import generate_rule_suggestion
    try:
        suggestion = await generate_rule_suggestion(body.description, exclude=body.exclude, scopes=body.scopes)
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
        raise HTTPException(422, "text 或 filename 不能为空")
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
    # 其余建议引擎阈值（原只能改 env 文件；此处补齐后管理台可调 + 落盘重启仍生效）
    AI_GATEWAY_SUGGEST_ERROR_MIN_CALLS: Optional[Union[int, Dict[str, int]]] = None
    AI_GATEWAY_KEYTIER_MIN_CALLS_BLACK: Optional[Union[int, Dict[str, int]]] = None
    AI_GATEWAY_KEYTIER_MIN_VIOLATIONS: Optional[Union[int, Dict[str, int]]] = None
    AI_GATEWAY_KEYTIER_VIOLATION_RATE: Optional[Union[float, Dict[str, float]]] = None
    AI_GATEWAY_KEYTIER_MIN_CALLS_WHITE: Optional[Union[int, Dict[str, int]]] = None
    AI_GATEWAY_KEYTIER_REVOKE_VIOLATIONS: Optional[Union[int, Dict[str, int]]] = None
    AI_GATEWAY_KEYTIER_WINDOW_HOURS: Optional[float] = Field(default=None, gt=0, le=8760)
    AI_GATEWAY_SUGGEST_REORDER_MIN_HITS: Optional[Union[int, Dict[str, int]]] = None
    # 规则7 的两个 AND 条件：累计调用数（0 = 该条件不参与判断）、峰值分钟调用数
    AI_GATEWAY_SUGGEST_LIMIT_MIN_CALLS: Optional[Union[int, Dict[str, int]]] = None
    # 加限流建议的 AND 条件二：峰值分钟调用数下限（0 = 关闭该条件）
    AI_GATEWAY_SUGGEST_LIMIT_MIN_PEAK: Optional[Union[int, Dict[str, int]]] = None
    # 建议条数上限：每规则 N 条 / 整卡 N 条
    AI_GATEWAY_SUGGEST_MAX_PER_RULE: Optional[int] = Field(default=None, ge=1, le=100)
    AI_GATEWAY_SUGGEST_MAX_TOTAL: Optional[int] = Field(default=None, ge=1, le=200)
    # 规则4/5/6 独立窗口（0 = 跟随 KEY 分级窗口）
    AI_GATEWAY_SUGGEST_RULE_WINDOW_HOURS: Optional[float] = Field(default=None, ge=0, le=8760)
    # 运行态维度规则阈值（0 = 关闭对应规则）
    AI_GATEWAY_SUGGEST_LATENCY_P95_MS: Optional[int] = Field(default=None, ge=0, le=600000)
    AI_GATEWAY_SUGGEST_DEGRADE_RATE: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    AI_GATEWAY_SUGGEST_TOKEN_GROWTH: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    # L2 灰区下限（即时生效；上限 20：再高则 25/30 档关键词单命中不再进灰区）
    AI_GATEWAY_L2_GRAY_FLOOR: Optional[int] = Field(default=None, ge=0, le=20)
    AI_GATEWAY_SUGGEST_SILENT_GRACE_DAYS: Optional[int] = Field(default=None, ge=0)


def _ser_base(v, lo, hi, key: str, cast=int) -> str:
    """次数类阈值：一律存**基准值**（24 小时窗口口径），各窗口的等比放大由后端算。

    dict 形态（历史分档值）取 **24h 档**当基准 ⇒ 老配置不用手改，保存后自动收敛成基准值。
    """
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        x = cast(v)
    else:
        d = {}
        for h, val in (v or {}).items():
            try:
                d[int(h)] = val
            except (TypeError, ValueError):
                raise HTTPException(422, f"{key} 窗口/取值非法")
        if not d:
            raise HTTPException(422, f"{key} 不能为空")
        x = cast(d.get(24, d[min(d, key=lambda k: abs(k - 24))]))
    if not (lo <= x <= hi):
        raise HTTPException(422, f"{key} 取值须 {lo}-{hi}")
    return x


class L2PromptsPutReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 空串表示"还原默认"
    AI_GATEWAY_L2_SYSTEM_PROMPT: Optional[str] = Field(default=None, max_length=8000)
    AI_GATEWAY_L2_USER_PROMPT_TEMPLATE: Optional[str] = Field(default=None, max_length=2000)


@router.get("/l2-config")
def l2_config_get():
    from src.gateway import small_model as _sm
    from src.gateway import stats_service as _st
    from src.gateway import main as _m
    from src.gateway.main import wl_l2_sample as _wl
    th = _st._thresholds()
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
        "AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD": th["violation_base"],
        "AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD": th["volume_base"],
        "AI_GATEWAY_SUGGEST_ERROR_RATE": th["error_rate"],
        # 建议引擎其余阈值（与 _thresholds() 同键，弹窗一次取全）
        # 次数类一律回传**基准值**（24h 口径），各窗口的等比放大由后端算（UI 只显示）
        "AI_GATEWAY_SUGGEST_ERROR_MIN_CALLS": th["error_min_calls_base"],
        "AI_GATEWAY_KEYTIER_MIN_CALLS_BLACK": th["kt_black_calls_base"],
        "AI_GATEWAY_KEYTIER_MIN_VIOLATIONS": th["kt_black_min_violations_base"],
        "AI_GATEWAY_KEYTIER_VIOLATION_RATE": th["kt_violation_rate"],
        "AI_GATEWAY_KEYTIER_MIN_CALLS_WHITE": th["kt_white_calls_base"],
        "AI_GATEWAY_KEYTIER_REVOKE_VIOLATIONS": th["kt_revoke_violations_base"],
        "AI_GATEWAY_KEYTIER_WINDOW_HOURS": th["kt_window_hours"],
        "AI_GATEWAY_SUGGEST_REORDER_MIN_HITS": th["reorder_min_hits_base"],
        "AI_GATEWAY_SUGGEST_LIMIT_MIN_CALLS": th["limit_min_calls"],
        "AI_GATEWAY_SUGGEST_LIMIT_MIN_PEAK": th["limit_min_peak"],
        "AI_GATEWAY_SUGGEST_MAX_PER_RULE": th["max_per_rule"],
        "AI_GATEWAY_SUGGEST_MAX_TOTAL": th["max_total"],
        "AI_GATEWAY_SUGGEST_RULE_WINDOW_HOURS": th["rule_window_hours"],
        "AI_GATEWAY_SUGGEST_LATENCY_P95_MS": th["latency_p95_ms"],
        "AI_GATEWAY_SUGGEST_DEGRADE_RATE": th["degrade_rate"],
        "AI_GATEWAY_SUGGEST_TOKEN_GROWTH": th["token_growth"],
        "AI_GATEWAY_L2_GRAY_FLOOR": _m._l2_gray_floor(),
        "AI_GATEWAY_SUGGEST_SILENT_GRACE_DAYS": th["silent_grace_days"],
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
            raise HTTPException(422, "L2 后端只能是 qwen 或 laya")
        if k == "AI_GATEWAY_L2_BACKEND":
            v = str(v).strip().lower()
        if k == "AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD":
            v = _ser_base(v, 1, 1000, k)
        elif k == "AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD":
            v = _ser_base(v, 1, 100000, k)
        elif k == "AI_GATEWAY_SUGGEST_ERROR_MIN_CALLS":
            v = _ser_base(v, 1, 100000, k)
        elif k in ("AI_GATEWAY_KEYTIER_MIN_CALLS_BLACK", "AI_GATEWAY_KEYTIER_MIN_CALLS_WHITE"):
            v = _ser_base(v, 1, 1000000, k)
        elif k in ("AI_GATEWAY_KEYTIER_MIN_VIOLATIONS", "AI_GATEWAY_KEYTIER_REVOKE_VIOLATIONS"):
            v = _ser_base(v, 0, 100000, k)
        elif k == "AI_GATEWAY_KEYTIER_VIOLATION_RATE":
            v = _ser_base(v, 0.0, 1.0, k, float)
        elif k == "AI_GATEWAY_SUGGEST_REORDER_MIN_HITS":
            v = _ser_base(v, 1, 100000, k)
        elif k == "AI_GATEWAY_SUGGEST_LIMIT_MIN_CALLS":
            v = _ser_base(v, 0, 10000000, k)
        elif k == "AI_GATEWAY_SUGGEST_LIMIT_MIN_PEAK":
            v = _ser_base(v, 0, 100000, k)
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


def _save_prompts(system, template, operator="", source="save"):
    """L2 提示词写入：先快照现状（可反悔）→ 内存即时覆盖 → 落盘。
    None=不动，""=还原默认。"""
    from src.gateway import small_model as _sm
    if system is None and template is None:
        raise HTTPException(422, "没有可保存的内容")
    get_admin_store().save_prompt_version(
        _sm.get_system_prompt(), _sm.get_user_prompt_template(),
        operator=operator, source=source)
    _sm.set_prompt_overrides(system=system, template=template)
    to_write, to_delete = {}, set()
    for k, v in (("AI_GATEWAY_L2_SYSTEM_PROMPT", system),
                 ("AI_GATEWAY_L2_USER_PROMPT_TEMPLATE", template)):
        if v is None:
            continue
        if v == "":
            to_delete.add(k)
        else:
            to_write[k] = v
    _l2ov.save_overrides(to_write, delete=to_delete)
    get_admin_store().log_ops(f"l2_prompts_{source}",
                              f"system={'set' if system else 'keep'} template={'set' if template else 'keep'}",
                              operator)
    return {
        "ok": True,
        "system_prompt": _sm.get_system_prompt(),
        "user_prompt_template": _sm.get_user_prompt_template(),
        "persisted": _l2ov.load_overrides(),
    }


@router.put("/l2-prompts")
def l2_prompts_put(req: L2PromptsPutReq, request: Request):
    """更新 L2 提示词：空串清空（还原默认）；非空即时生效 + 落盘 l2_overrides.yaml。写前快照，可回滚。"""
    return _save_prompts(req.AI_GATEWAY_L2_SYSTEM_PROMPT, req.AI_GATEWAY_L2_USER_PROMPT_TEMPLATE,
                         operator=_operator(request))


@router.get("/l2-prompt-versions")
def l2_prompt_versions_api(limit: int = 50):
    """L2 提示词历史版本（每次 PUT 前快 snapshot；system 只给 200 字预览 + sha12）。"""
    items = get_admin_store().list_prompt_versions(limit)
    for r in items:
        r["ts"] = to_beijing(r.get("ts"))
    return {"items": items}


@router.get("/l2-prompt-versions/{vid}")
def l2_prompt_version_api(vid: int):
    v = get_admin_store().get_prompt_version(vid)
    if v is None:
        raise HTTPException(404, "版本不存在")
    return v


@router.post("/l2-prompt-versions/{vid}/rollback")
def l2_prompt_version_rollback_api(vid: int, request: Request):
    """回滚到指定版本（现状再快照，可再反悔；即时生效 + 落盘）。"""
    v = get_admin_store().get_prompt_version(vid)
    if v is None:
        raise HTTPException(404, "版本不存在")
    return _save_prompts(v["system_prompt"] or "", v["user_template"] or "",
                         operator=_operator(request), source="rollback")


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
def l2_shadow_stats(hours: int = 168, since: str = "", until: str = ""):
    """影子（laya）汇总：扫审计热缓存（Redis 近 5000 条），按窗口聚合主/影判定对照。

    口径：
    - l2_total：窗口内带 l2 判定的条数（只有进 L2 的灰区流量才有；L1 直判无 l2）。
    - shadowed：l2 含 shadow_label 的条数；shadow_degraded：影子失败只丢影子。
    - 一致率 = 主影 label 相同 / 可比条数（主判定 degraded 的不计入可比，兜底 label 不可比）。
    - cells：主密影密(cc)/主密影放(cn)/主放影密(nc)/主放影放(nn)。
    - until：显式上界（UTC，灰区灌数轮次用；见 /l2-rounds）。默认无上界。
    """
    import datetime as _dt

    from src.gateway.audit_store import get_audit_store

    try:
        hours = max(1, min(int(hours), 720))
    except (TypeError, ValueError):
        hours = 168
    cutoff = _cutoff(hours, since)
    hi = None
    if until:
        try:
            hi = _dt.datetime.strptime(str(until).strip()[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            hi = None

    def _ts(e) -> object:
        # fromisoformat 而非 strptime：审计尾 5000 条每条都要过这里，实测
        # strptime 230ms/5万次 vs fromisoformat 7ms（33×，标准库零依赖、同日历语义）。
        # /l2-rounds 循环调本函数 N 次，这是该端点 265ms 的全部来源。
        try:
            return _dt.datetime.fromisoformat(
                str(e.get("time") or e.get("ts") or "").strip().replace("T", " ")[:19])
        except Exception:
            return None

    try:
        entries = get_audit_store().tail(5000) or []
    except Exception:
        entries = []
    scanned = len(entries)
    # 每条只解析一次时间戳。原写法在列表推导里对同一条调 _ts() 达 3 次
    # （两个条件各一次），5000 条 = 15000 次 strptime，单次调用 24ms；
    # /l2-rounds 循环调本函数 N 次（N=轮次数），直接是端点耗时的全部来源。
    window = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        t = _ts(e)
        if (t or cutoff) < cutoff:
            continue
        if hi is not None and (t or hi) > hi:
            continue
        window.append(e)

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


# ---------------- L2 影子看板：趋势 / 分歧样本 / 标注 / 导出（数据源 MySQL audit_logs.l2 列） ----------------
# l2 列 2026-09-28 才加（此前影子数据只在 Redis 热缓存 5000 条）；
# ts 存 UTC，cutoff 一律 Python utcnow()，禁用 SQL NOW()（北京时间，窗口恒空）。

def _cutoff(hours: int, since: str = ""):
    """窗口起点（UTC）：since 显式给（"本轮灌数"用，gray_flood 写的轮次起点）优先于 hours。"""
    import datetime as _dt
    if since:
        try:
            return _dt.datetime.strptime(str(since).strip()[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - _dt.timedelta(hours=hours)


def _l2_window_rows(hours: int, since: str = ""):
    """窗口内带 l2 判定的审计行（audit_logs.l2 非空），返回 [(row, l2_dict)]，oldest-first。"""
    from src.gateway.audit_store import get_audit_store

    rows = get_audit_store().query_mysql(since=_cutoff(hours, since), limit=5000) or []
    return [(r, r["l2"]) for r in rows if isinstance(r.get("l2"), dict) and r.get("l2")]


L2_ROUND_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "var", "l2-round.json")
L2_ROUNDS_FILE = os.path.join(os.path.dirname(L2_ROUND_FILE), "l2-rounds.jsonl")


@router.get("/l2-round")
def l2_round():
    """最近一轮灰区灌数标记（scripts/gray_flood.py 写）：{since, started_at, sent, finished_at}。

    「本轮」窗口用：固定小时窗会把两轮灌数装进同一个窗，同一文本就成了 dup×2（看着像重复分歧）。
    """
    try:
        with open(L2_ROUND_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"since": ""}
    except Exception as e:  # noqa: BLE001
        return {"since": "", "error": str(e)[:120]}


@router.get("/l2-rounds")
def l2_rounds(limit: int = 30):
    """灰区灌数轮次历史（var/l2-rounds.jsonl，gray_flood 每轮结束追加一行），倒序。

    为什么要有这个：判定流量是**脉冲式的**（一轮 50 条集中在 40 秒内，其余时间零流量），
    按小时窗看会误读成"没数据/一致率变差"。轮次是这批流量真实的计量单位。
    每行的 l2_total/agree/cells 是该轮 [started, finished] 区间内的实算值（扫 Redis 热缓存）。
    """
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 30
    try:
        with open(L2_ROUNDS_FILE, encoding="utf-8") as f:
            rounds = [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        return {"rounds": [], "source": "none"}
    except Exception as e:  # noqa: BLE001
        return {"rounds": [], "error": str(e)[:120]}
    out = []
    for r in rounds[-limit:][::-1]:
        s = l2_shadow_stats(hours=720, since=r.get("started", ""), until=r.get("finished", ""))
        out.append({"started": r.get("started", ""), "finished": r.get("finished", ""),
                    "sent": r.get("sent"), "laya": r.get("laya", ""),
                    "l2_total": s["l2_total"], "compared": s["compared"],
                    "agree": s["agree"], "agree_rate": s["agree_rate"],
                    "cells": s["cells"]})
    return {"rounds": out, "total": len(rounds)}


def _div_group_key(x: dict) -> str:
    """分歧去重组键：同文本+文件名（**不含方向**）。label/divergent 两处共用，改口径只改这里。

    不含 cell：主/影任一方升级后同一条样本可能从 cn 翻成 nc（prompt/laya 改版就会发生），
    键带方向会把它裂成两条独立行——旧标跟旧行、新行显示未标，看着像凭空多一条新分歧。
    标注是关于文本的判断，与当次谁判密无关，故方向不进键；行上仍显示最新方向。"""
    import hashlib as _hl
    import re as _re
    return _hl.sha1((_re.sub(r"\s+", " ", x.get("text_preview") or "").strip() + "|"
                     + (x.get("filename") or "")).encode()).hexdigest()


def _l2_divergence_rows(hours: int, since: str = ""):
    """主/影分歧行（可比：主非降级且双方 label 齐全），nc=主放影密 / cn=主密影放。"""
    out = []
    for r, l2 in _l2_window_rows(hours, since):
        ml, sl = l2.get("label"), l2.get("shadow_label")
        if sl not in ("CONFIDENTIAL", "NORMAL") or ml not in ("CONFIDENTIAL", "NORMAL") \
                or l2.get("degraded") or ml == sl:
            continue
        out.append({
            "req_id": r.get("req_id") or "",
            "ts": str(r.get("ts") or ""),
            "cell": "nc" if ml == "NORMAL" else "cn",
            "main": ml,
            "shadow": sl,
            "shadow_prob": (l2.get("shadow_probabilities") or {}).get("CONFIDENTIAL"),
            "text_preview": (r.get("text_preview") or "")[:500],
            "filename": r.get("filename") or "",
        })
    return out


@router.get("/l2-divergences")
def l2_divergences(hours: int = 168, cell: str = "", limit: int = 200, q: str = "", since: str = ""):
    """主/影分歧样本清单（nc/cn），join l2_labels 标注状态。"""
    try:
        hours = max(1, min(int(hours), 720))
    except (TypeError, ValueError):
        hours = 168
    limit = max(1, min(int(limit or 200), 500))
    rows = _l2_divergence_rows(hours, since)
    if cell in ("nc", "cn"):
        rows = [x for x in rows if x["cell"] == cell]
    if q:
        qq = q.strip().lower()
        rows = [x for x in rows
                if qq in (x["text_preview"] or "").lower() or qq in (x["filename"] or "").lower()]
    rows.sort(key=lambda x: x["ts"], reverse=True)
    # 去重 collapse：同文本+文件名+方向只留最新一条（灌数/重试常有成对重复），dup 计折叠数。
    # 附带防双标：同一文本只剩一个标注入口（手册 0/1 双标即双行所致）。
    # 标注继承组内最新已标（_ids 按 ts 倒序=最新在前）：再灌的同文本新行未标也不盖掉旧标注。
    seen: dict[str, dict] = {}
    for x in rows:
        k = _div_group_key(x)
        if k in seen:
            seen[k]["dup"] += 1
            seen[k]["_ids"].append(x["req_id"])
        else:
            x["dup"] = 1
            x["_ids"] = [x["req_id"]]
            seen[k] = x
    rows = list(seen.values())
    rows.sort(key=lambda x: x["ts"], reverse=True)
    labels = get_admin_store().get_l2_labels([i for x in rows for i in x["_ids"]])
    for x in rows:
        x["label"] = next((labels[i] for i in x["_ids"] if labels.get(i) is not None), None)
        del x["_ids"]
    return {"hours": hours, "total": len(rows), "items": rows[:limit]}


class L2LabelReq(BaseModel):
    req_id: str = Field(default="", max_length=40)
    label: Optional[int] = Field(default=None, ge=0, le=1)  # 1=机密 0=非密 null=取消标注


@router.post("/l2-divergences/label")
def l2_divergences_label(req: L2LabelReq):
    """标注分歧样本（v10 训练原料）。label=null 取消标注。同组（同文本同向）全组联动，
    新灌重复行自带同标，显示层继承（l2_divergences）兜底历史数据。"""
    if not req.req_id:
        raise HTTPException(422, "req_id 不能为空")
    store = get_admin_store()
    try:
        allrows = _l2_divergence_rows(720)
        tk = next((_div_group_key(x) for x in allrows if x["req_id"] == req.req_id), None)
        ids = [x["req_id"] for x in allrows if tk is not None and _div_group_key(x) == tk]
    except Exception:
        ids = []
    for i in ids or [req.req_id]:
        store.upsert_l2_label(i, req.label)
    return {"ok": True, "labeled": len(ids) if ids else 1}


@router.get("/l2/auto-label/models")
def l2_auto_label_models():
    """自动标注可选模型：只列内网 chat provider（分歧样本是真实审计内容，不出境）。"""
    from src.gateway import providers as _prov
    out = [{"value": n, "label": f"{n}（{p.default_model or '?'}）"}
           for n, p in (_prov.load_routing().providers or {}).items()
           if getattr(p, "local", False) and getattr(p, "kind", "chat") == "chat"]
    return {"models": out}


class AutoLabelReq(BaseModel):
    req_ids: list[str] = Field(default_factory=list)
    model: str = Field(default="vllm_local", max_length=64)


@router.post("/l2-divergences/auto-label")
def l2_divergences_auto_label(req: AutoLabelReq):
    """内网模型给分歧样本出标注**建议**（不写库，人工采纳才走 /label）。

    l2_labels 是训练真值（标错即投毒），模型判断不能直接落库；这里只返回
    {req_id, label, confidence, reason} 草稿，由页面「采纳」触发正式写入。"""
    from src.gateway import l2_retrain as _l2r
    from src.gateway import providers as _prov

    provider = (_prov.load_routing().providers or {}).get(req.model)
    if provider is None:
        raise HTTPException(404, f"provider 不存在：{req.model}")
    if not provider.local or getattr(provider, "kind", "chat") != "chat":
        raise HTTPException(400, "自动标注只允许内网 chat 模型（审计文本不出境）")

    ids = {x for x in req.req_ids if x}
    rows = [x for x in _l2_divergence_rows(720) if not ids or x["req_id"] in ids]
    seen: dict[str, dict] = {}
    for x in rows:                      # 同文本只问一次（与列表去重口径一致）
        seen.setdefault(_div_group_key(x), x)
    rows = list(seen.values())[:60]     # 单次上限 60：并发 4、单条 ~2s
    if not rows:
        return {"total": 0, "items": []}
    items = [{"req_id": x["req_id"], "text": x["text_preview"], "filename": x["filename"]}
             for x in rows]
    return {"total": len(items), "items": _l2r.auto_label(items, provider)}


@router.get("/l2-divergences/export")
def l2_divergences_export(hours: int = 168, cell: str = "", labeled: str = "all", since: str = ""):
    """导出分歧样本 JSONL（含标注），喂 build_laya_dataset / 离线分析。

    since 与列表端点同参：管理台按灌数轮次窗（since+hours=24）导出时口径才与表一致。"""
    try:
        hours = max(1, min(int(hours), 720))
    except (TypeError, ValueError):
        hours = 168
    rows = _l2_divergence_rows(hours, since)
    if cell in ("nc", "cn"):
        rows = [x for x in rows if x["cell"] == cell]
    labels = get_admin_store().get_l2_labels([x["req_id"] for x in rows])
    for x in rows:
        x["label"] = labels.get(x["req_id"])
    if labeled == "labeled":
        rows = [x for x in rows if x["label"] is not None]
    elif labeled == "unlabeled":
        rows = [x for x in rows if x["label"] is None]
    rows.sort(key=lambda x: x["ts"])
    body = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows)
    return Response(content=body, media_type="application/jsonl",
                    headers={"Content-Disposition":
                             f'attachment; filename="l2-divergences-{hours}h.jsonl"'})


@router.get("/events")
def events_api(hours: int = 24):
    """降级事件流：流式降级 downgraded_from / 本地路由 route_local / L2 不可用 l2_unavailable。
    数据源 audit_logs（ts 存 UTC）。熔断器状态另看 /circuit。"""
    import datetime as _dt
    from src.gateway.audit_store import get_audit_store

    try:
        hours = max(1, min(int(hours), 720))
    except (TypeError, ValueError):
        hours = 24
    since = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - _dt.timedelta(hours=hours)
    rows = get_audit_store().query_mysql(since=since, limit=5000) or []
    start = since.replace(minute=0, second=0, microsecond=0)
    by_hour: dict[str, dict] = {}
    for i in range(hours + 1):
        t = (start + _dt.timedelta(hours=i)).strftime("%Y-%m-%d %H:00")
        by_hour[t] = {"t": t, "degrade": 0, "route_local": 0, "l2_unavailable": 0}
    summary = {"degrade": 0, "route_local": 0, "l2_unavailable": 0}
    recent = []
    for r in rows:
        kinds = []
        if r.get("downgraded_from"):
            kinds.append("degrade")
        if r.get("action") == "route_local":
            kinds.append("route_local")
        if r.get("rule") == "l2_unavailable":
            kinds.append("l2_unavailable")
        if not kinds:
            continue
        try:
            ts = _dt.datetime.strptime(str(r.get("ts") or "")[:16], "%Y-%m-%d %H:%M")
        except Exception:
            continue
        b = by_hour.get(ts.strftime("%Y-%m-%d %H:00"))
        for k in kinds:
            summary[k] += 1
            if b is not None:
                b[k] += 1
        recent.append({
            "ts": str(r.get("ts") or ""),
            "kinds": kinds,
            "action": r.get("action") or "",
            "rule": r.get("rule") or "",
            "downgraded_from": r.get("downgraded_from") or "",
            "provider": r.get("provider") or "",
            "model": r.get("model") or "",
            "text_preview": (r.get("text_preview") or "")[:80],
            "filename": r.get("filename") or "",
        })
    recent.sort(key=lambda x: x["ts"], reverse=True)
    return {"hours": hours, "summary": summary, "by_hour": list(by_hour.values()),
            "recent": recent[:100]}


@router.get("/rule-hits")
def rule_hits_api(hours: int = 168):
    """规则命中统计：audit_logs 窗口按 rule 聚合（排除 默认放行，含改名前旧名 default_allow
    的历史行）。L2 判决（l2_confidential/l2_unavailable）与系统内置 fail-safe 判定
    （encoded_keyword/review_text_overflow/first_chunk_fallback/prompt_too_long_fallback）
    都计入——审计页要显示，规则命中榜保持同一集合，界面上由 ruleZh 标「（系统内置）」，
    用户一眼能看出它不是可配置规则。带 action 分布 + 最后命中（UTC）。
    沉默规则判断的页面内证据（建议规则 5）。
    2026-10-14 规则名改中文：改名前历史行按旧英文名出现，窗口滑过后自然消失。"""
    import datetime as _dt
    from src.gateway.audit_store import get_audit_store

    try:
        hours = max(1, min(int(hours), 720))
    except (TypeError, ValueError):
        hours = 168
    since = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - _dt.timedelta(hours=hours)
    rows = get_audit_store().query_mysql(since=since, limit=5000) or []
    agg: dict[str, dict] = {}
    total = 0
    for r in rows:
        name = r.get("rule") or ""
        if not name or name in ("default_allow", "默认放行"):
            continue
        total += 1
        a = agg.setdefault(name, {"rule": name, "count": 0, "actions": {}, "last_seen": ""})
        a["count"] += 1
        act = r.get("action") or ""
        a["actions"][act] = a["actions"].get(act, 0) + 1
        ts = str(r.get("ts") or "")
        if ts > a["last_seen"]:
            a["last_seen"] = ts
    items = sorted(agg.values(), key=lambda x: -x["count"])
    for a in items:
        a["last_seen"] = to_beijing(a["last_seen"])
    return {"hours": hours, "total": total, "items": items}


# ---------------- L2 一键优化管道：人工标注 → 数据集 → 微调 → 候选评测 → 晋升影子 ----------------
# 权威 backend 切换（qwen→laya）永不走这里，仍人工拍板。

class L2PromoteReq(BaseModel):
    version: int = Field(ge=1, le=9999)


@router.post("/l2/retrain")
def l2_retrain_api():
    """启动优化训练（后台 ~10-20min：数据集→GPU6 微调→候选评测）。标注不足 422，在跑 409。"""
    from src.gateway import l2_retrain
    try:
        return l2_retrain.start()
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except (FileNotFoundError, OSError) as e:
        raise HTTPException(503, str(e))


@router.get("/l2/retrain/status")
def l2_retrain_status_api():
    from src.gateway import l2_retrain
    return l2_retrain.status()


@router.get("/l2/retrain/versions")
def l2_retrain_versions_api():
    """盘上可用 checkpoint 版本 + 当前影子版本（晋升下拉用）。"""
    from src.gateway import l2_retrain
    return l2_retrain.versions()


@router.get("/l2/retrain/reports")
def l2_retrain_reports_api():
    """各影子版本盘上评估报告（三门 + 回归 diff）：管理台「版本评估」卡数据源。"""
    from src.gateway import l2_retrain
    return l2_retrain.list_reports()


class PromptTestReq(BaseModel):
    system_prompt: str = Field(min_length=20, max_length=8000)


@router.post("/l2/retrain/prompt-test")
def l2_retrain_prompt_test_api(req: PromptTestReq):
    """候选 system prompt 打分：同 battery 打 qwen，不落地（~30-60s）。
    通过后再 PUT /l2-prompts 应用（即时生效 + 落盘 l2_overrides.yaml）。"""
    from src.gateway import l2_retrain
    try:
        return l2_retrain.prompt_test(req.system_prompt)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/l2/retrain/prompt-propose")
def l2_retrain_prompt_propose_api():
    """qwen 自省修 prompt：主判错题（已标×与主判相反）→ qwen 提追加补丁 → 同 battery 打基线+候选。
    只生成不应用（~2min）；应用仍走 PUT /l2-prompts 人工确认（权威链不动自动门）。"""
    from src.gateway import l2_retrain
    try:
        return l2_retrain.propose_prompt_fix()
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.get("/l2/retrain/prompt-propose")
def l2_retrain_prompt_propose_latest_api():
    """最近一次候选 prompt 修补（盘上持久化）：重启/切页后前端可恢复显示。"""
    from src.gateway import l2_retrain
    return {"proposal": l2_retrain.latest_prompt_fix()}


class HardAddReq(BaseModel):
    items: list = Field(default_factory=list)  # [{text, label}]，label 取错题的 want


@router.post("/l2/retrain/hard")
def l2_retrain_hard_api(req: HardAddReq):
    """错题一键入 hard.jsonl（下版训练自动加权×4）。文件在 ~/models，永不入库。"""
    from src.gateway import l2_retrain
    return l2_retrain.add_hard(req.items)


@router.post("/l2/retrain/promote")
def l2_retrain_promote_api(req: L2PromoteReq):
    """候选 checkpoint 晋升为影子模型（env 文件 + 无密重启 laya-l2 + 身份核对）。"""
    from src.gateway import l2_retrain
    try:
        return l2_retrain.promote(req.version)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except (RuntimeError, ValueError) as e:
        raise HTTPException(409, str(e))


# ---------------- 运维诊断：路由检查器 / provider 健康检查 / 熔断器 ----------------

class RouteInspectFile(BaseModel):
    filename: str = Field(default="upload", max_length=256)
    file_data: str = Field(default="", max_length=12_000_000)  # data-URL 或裸 base64，约 9MB 原字节


class RouteInspectReq(BaseModel):
    model: str = Field(default="", max_length=256)
    text: str = Field(default="", max_length=20000)
    channel: str = Field(default="", max_length=16)  # ""=纯文本；check | chat | responses
    files: List[RouteInspectFile] = Field(default=[])


def _route_inspect_blind_fields(policies, has_files: bool, channel: str) -> List[str]:
    """本次干跑无法生效的规则维度（只回「当前策略真的用到了」的那些）。

    干跑不产生请求侧上下文：会话粘性/污染标记恒假，model/client/hour 不进 ctx；
    file.watermark 任何通道都不填；check 通道 file.ext 恒空。规则若写了这些条件，
    在这里必然不命中——必须明说，否则会被读成「规则写错了/没生效」。
    """
    from src.gateway.policy_admin import _COND_RE
    refs: set = set()

    def walk(w) -> None:
        if not isinstance(w, dict):
            return
        for k, v in w.items():
            if k in ("any", "all") and isinstance(v, list):
                for c in v:
                    walk(c)
            else:
                m = _COND_RE.fullmatch(str(k).strip())
                if m:
                    refs.add(m.group("field"))

    for p in policies:
        walk(p.get("when") if hasattr(p, "get") else getattr(p, "when", None))
    blind = {"session.confidential", "tainted_file.hit", "model", "client.key",
             "time.hour", "file.watermark"}
    if not has_files:
        # 纯文本干跑没有文件字节：文件解析类维度全空
        blind |= {"file.sheet_names", "file.headers", "file.ext", "file.text",
                  "findings.unparsed_binary", "findings.parse_failed",
                  "findings.ocr_empty_and_image"}
    elif channel == "check":
        blind.add("file.ext")
    return sorted(refs & blind)


def _route_inspect_finish(decision, l1_out, l2_out, files_out, channel, model, _rt, provider_keys,
                          shadow=None, uncovered=None):
    """route-inspect 收口：resolve 去向 + 统一响应（含 files/channel/shadow 归因）。"""
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
        "shadow": shadow,
        "final_action": decision.get("action"),
        "final_rule": decision.get("name"),
        "routing": rout,
        "channel": channel,
        "files": files_out,
        "uncovered": uncovered or [],
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
    from src.gateway import small_model as _sm

    async def _shadow_of(t: str, fn: str = ""):
        """归因用影子：与线上影子同调用直问 laya（bypass 缓存/熔断，看活模型真实判定），
        2s 短超时；失败只记 degraded，不挡干跑、不落审计、不限流。"""
        try:
            sh = await _sm._call_laya(t, fn, [], [], timeout=2.0)
        except Exception as e:
            return {"degraded": str(e)[:120]}
        if sh.get("degraded"):
            return {"degraded": sh.get("error") or sh.get("degraded_reason") or "error"}
        return {"label": sh.get("label"), "confidence": sh.get("confidence"),
                "reason": sh.get("reason"), "latency_ms": sh.get("latency_ms")}

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
    # 本次干跑覆盖不到的规则维度（只列当前策略真用到的），随响应回给 UI
    uncovered = _route_inspect_blind_fields(_pol.load_policy(), bool(files), channel)

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
                        "sheet_names": sheets, "text": ftext,
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
            inline_text, inline_bad, _, _ = await _m._inline_media_scan("chat", [{"role": "user", "content": blocks}])
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
            inline_text, inline_bad, _, _ = await _m._inline_media_scan("responses", rbody)
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
        _gray0 = _m._l2_always() or _m._l2_gray_trigger(findings0)
        l2_out = {"triggered": bool(l2r0),
                  "trigger": "gray" if l2r0 else None,
                  "latency_ms": (l2r0 or {}).get("latency_ms"),
                  "label": (l2r0 or {}).get("label"),
                  "confidence": (l2r0 or {}).get("confidence"),
                  "reason": (l2r0 or {}).get("reason"),
                  "degraded": _m.is_degraded(l2r0) if l2r0 else False}
        if not l2r0:
            l2_out["skipped"] = ("L1 未放行" if l1["action"] != "allow"
                                 else "未进灰区（L2 只看 risk ≥ 灰区下限，无上界，不计字数）")
        decision = decision0
        _fn = " ".join((f.filename or "").strip() for f in files if (f.filename or "").strip())
        return _route_inspect_finish(decision, l1_out, l2_out, files_out, channel or "text",
                                     body.model, _rt, provider_keys,
                                     shadow=await _shadow_of(text, _fn), uncovered=uncovered)

    if channel == "check" and files:
        # ctx/findings 已在上分支建成（含文件合并）；文件式 L2（files/check 同款：allow 即审，分块聚合）
        l1 = _pol.decide(_pol.load_policy(), ctx)
        _enc = _m._encoded_keyword_decision(findings)
        if _enc and l1.get("action") == "allow":
            l1 = _enc
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
            _gray = _m._l2_always() or _m._l2_gray_trigger(findings)
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
                l2_out["skipped"] = "未进灰区（L2 只看 risk ≥ 灰区下限，无上界，不计字数）"
        else:
            l2_out["skipped"] = "L1 未放行"
        return _route_inspect_finish(decision, l1_out, l2_out, files_out, channel,
                                     body.model, _rt, provider_keys,
                                     shadow=await _shadow_of(
                                         text, (file_l2 or {}).get("filename", "")),
                                     uncovered=uncovered)

    # 纯文本旧逻辑（零改动）
    findings = _insp.inspect_text(text) if text else {}
    ctx = {"text": text, "file": {}, "findings": findings,
           "session": {"confidential": False}}
    l1 = _pol.decide(_pol.load_policy(), ctx)
    _enc = _m._encoded_keyword_decision(findings)
    if _enc and l1.get("action") == "allow":
        l1 = _enc
    l1_out = {
        "rule": l1.get("name"),
        "action": l1.get("action"),
        "reason": l1.get("reason") or "",
        "findings": {k: v for k, v in (findings or {}).items() if v},
    }

    decision = l1
    l2_out: dict = {"triggered": False}
    _gray = _m._l2_always() or _m._l2_gray_trigger(findings)
    if l1.get("action") == "allow" and _gray:
        t0 = _t.monotonic()
        # 与线上 chat 同一口径：按 scope 配置取块（long_text 会展开成多段），
        # 否则干跑只审 text 单块，长文覆盖率被低估。
        _blocks = _m._select_l2_scopes("chat", [{"role": "user", "content": text}], text)
        if not _blocks:
            _blocks = [("last_user", text)]
        if len(_blocks) == 1:
            res = await _m.small_classify(_blocks[0][1], filename="", headers=[], sheet_names=[])
        else:
            res = await _m.classify_chunks([t for _, t in _blocks],
                                           filename="", headers=[], sheet_names=[],
                                           scopes=[s for s, _ in _blocks])
        if isinstance(res, dict):
            res["scope"] = (res.get("scope") or _blocks[0][0]) if _blocks else None
            res["scopes_run"] = [s for s, _ in _blocks]
            res.setdefault("degraded_scopes", [])
        l2_out = {
            "triggered": True,
            "trigger": "gray",
            "latency_ms": round((_t.monotonic() - t0) * 1000),
            "label": (res or {}).get("label"),
            "confidence": (res or {}).get("confidence"),
            "reason": (res or {}).get("reason"),
            "degraded": _m.is_degraded(res),
            "scope": (res or {}).get("scope"),
            "scopes_run": (res or {}).get("scopes_run"),
            "degraded_scopes": (res or {}).get("degraded_scopes"),
        }
        d = _m._l2_decision(res)
        if d is not None:
            decision = d
    else:
        l2_out["skipped"] = ("L1 未放行" if l1.get("action") != "allow"
                             else "未进灰区（L2 只看 risk ≥ 灰区下限，无上界，不计字数）")

    return _route_inspect_finish(decision, l1_out, l2_out, [], "text",
                                 body.model, _rt, provider_keys,
                                 shadow=await _shadow_of(text), uncovered=uncovered)


class ProviderCheckReq(BaseModel):
    name: str = Field(default="", max_length=128)
    timeout: float = Field(default=10.0, ge=1, le=30)


@router.post("/providers/check")
def providers_check_api(body: ProviderCheckReq):
    """provider 主动健康检查：GET {base_url}/models 测连通+鉴权+延迟。

    name 空 = 查全部；不发起真实模型调用（不耗 token）。探测逻辑复用 provider_probe，
    与后台周期探测同一份代码（口径不漂移）。"""
    from src.gateway import provider_probe
    from src.gateway.providers import load_routing

    cfg = load_routing()
    names = [body.name] if body.name else list(cfg.providers)
    results = []
    for name in names:
        prov = cfg.get(name)
        if prov is None:
            results.append({"name": name, "ok": False, "error": "未知 provider"})
            continue
        results.append(provider_probe.probe_one(prov, body.timeout))
    return {"results": results}


@router.get("/circuit")
def circuit_api():
    """熔断器逐 provider::model 明细（state 为已应用 OPEN→HALF_OPEN 自动迁移的有效态）。

    熔断键 = provider::model（别名组候选与点名请求共用一份状态），所以这里按
    「provider + 模型」两列展示；键里带 "::" 的是 provider::model，裸键是 L2
    小模型那种伪 provider（small_model）。
    """
    import time as _t

    from src.gateway.circuit_breaker import get_breaker, key_for

    b = get_breaker()
    now = _t.time()
    recovery = int(os.getenv("AI_GATEWAY_CB_RECOVERY_SECONDS", "30"))
    from src.gateway.providers import load_routing
    snap = b.snapshot()
    # 已知 (provider, 模型) 组合：routing 默认模型 + 别名组成员 + 外部候选 + 已有状态
    entries: dict = {}
    for k in snap:
        prov_name, sep, m = k.partition("::")
        entries[k] = (prov_name, m) if sep else (k, "")
    for name, p in load_routing().providers.items():
        dm = getattr(p, "default_model", "") or ""
        entries.setdefault(key_for(name, dm), (name, dm))
    try:
        from src.gateway.admin_store import get_admin_store
        for g in get_admin_store().list_alias_groups():
            for m in g.get("members") or []:
                entries.setdefault(key_for(m["provider"], m["model"]), (m["provider"], m["model"]))
    except Exception:
        pass
    try:
        from src.gateway.model_policy import external_candidates
        for c in external_candidates():
            entries.setdefault(key_for(c["provider"], c["model"]), (c["provider"], c["model"]))
    except Exception:
        pass
    providers = {}
    for key, (prov_name, m) in sorted(entries.items()):
        s = snap.get(key) or {"state": "closed", "opened_at": 0.0,
                              "last_failure": 0.0, "failure_count_window": 0,
                              "half_open_successes": 0}
        d = dict(s)
        d["provider"] = prov_name
        d["model"] = m
        d["state"] = b.state(key)
        d["cooldown_remaining_s"] = (
            round(max(0.0, recovery - (now - s["opened_at"])), 1)
            if s["state"] == "open" and s["opened_at"] else 0.0)
        providers[key] = d
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


# ---------------- 状态条 SSE ----------------
# 本文件其余端点都是 sync def（FastAPI 自动进线程池）。SSE 必须是 async 生成器，
# 所以聚合本身仍走 sync 路径，再 run_in_threadpool 挪出事件循环。

# 停机信号：uvicorn 0.30 对「未完成的流式响应」只置 keep_alive=False 不断连，
# 且 lifespan shutdown 排在连接排空之后 —— 永不结束的 SSE 生成器会把 stop 拖满
# systemd TimeoutStopSec=90s 然后 SIGKILL，浏览器端报 net::ERR_INCOMPLETE_CHUNKED_ENCODING。
# 修法：unit 的 ExecStop 先 kill -USR1 $MAINPID，生成器见
# sse_stop 正常 return（chunked 终止块照发），连接干净排空，进程快速自退。
sse_stop = asyncio.Event()


def register_sse_stop() -> None:
    """startup 钩子调：SIGUSR1 → sse_stop.set()。TestClient 的 portal 线程不是
    主线程，signal.signal 会抛 ValueError，测试态直接跳过。"""
    if threading.current_thread() is not threading.main_thread():
        return
    signal.signal(signal.SIGUSR1, lambda *_args: sse_stop.set())


def _status_snapshot() -> dict:
    """状态条要的四份原始数据，逐个复用现成端点逻辑（不复制计算）。"""
    from src.gateway import small_model as _sm
    from src.gateway.audit_store import get_audit_store
    from src.gateway.health_router import _health_payload

    _redact = lambda u: re.sub(r"://[^@/]*@", "://***@", str(u or ""))
    store = get_audit_store()
    return {
        "health": _health_payload(),
        "circuit": circuit_api(),
        # l2-config 端点还返回阈值/落盘配置，状态条用不到，这里只回三个开关
        "l2": {
            "SMALL_MODEL_ENABLED": _sm.l2_enabled(),
            "AI_GATEWAY_L2_BACKEND": _sm.l2_backend() or "qwen",
            "AI_GATEWAY_L2_SHADOW": (os.getenv("AI_GATEWAY_L2_SHADOW") or "").strip().lower() == "true",
        },
        "audit": {
            "backend": _redact(store.backend()),
            "redis": {"connected": store._r is not None, "url": _redact(store._r_url)},
            "mysql": store.mysql_status(),
        },
    }


@router.get("/status/stream")
async def status_stream():
    """管理台顶部状态条 SSE：每 60s 推一份 /health + 熔断 + L2 + 审计聚合。

    取代前端 4 个 fetch 的轮询；派生（五灯/覆盖率/异常判定）仍在前端算，
    与聚合口径同源。手动刷新 = 断开重连（EventSource 断线自带重连）。
    """
    from fastapi.responses import StreamingResponse
    from starlette.concurrency import run_in_threadpool

    async def gen():
        if sse_stop.is_set():
            return  # 停机窗口内的晚到重连：别开新流，让 EventSource 下轮再试
        while True:
            try:
                payload = await run_in_threadpool(_status_snapshot)
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception as e:  # 单次聚合失败不掐流，下一拍重试
                yield f"data: {json.dumps({'error': repr(e)[:200]}, ensure_ascii=False)}\n\n"
            try:
                await asyncio.wait_for(sse_stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                continue  # 到点没停机信号，下一拍
            return  # 停机：流正常收尾（chunked 终止块照发），EventSource 自动重连

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
