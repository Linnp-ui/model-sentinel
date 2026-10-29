"""审计路由：/admin/audit/entries。"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .audit_store import get_audit_store
from .stat_scope import is_local_route as _islr, to_beijing as _to_beijing
from datetime import datetime, timezone


def _admin_auth(request: Request):
    from .main import admin_auth as _aa
    return _aa(request)


def _admin_page_auth(request: Request):
    from .main import admin_page_auth as _apa
    return _apa(request)


def _m(name):
    from . import main as _main
    return getattr(_main, name)


router = APIRouter()


@router.get("/admin/audit/entries", dependencies=[Depends(_admin_page_auth)])
async def audit_entries_api(request: Request, ip=None, token=None, action=None, page=None, since=None, until=None, provider=None, rule=None, q=None, sort=None, shadow=None, status=None):
    _parse_window_ts = _m("_parse_window_ts")
    _enrich_audit_key_name = _m("_enrich_audit_key_name")
    _audit_filter_rows = _m("_audit_filter_rows")
    _entry_status = _m("_entry_status")
    _entry_blocked = _m("_entry_blocked")

    store = get_audit_store()
    limit = 50
    try:
        page = int(page)
    except Exception:
        page = 1
    page = max(1, page)
    fetch_n = 5000
    all_entries = store.tail(fetch_n)
    _enrich_audit_key_name(all_entries)
    since_dt = _parse_window_ts(since, naive_as_utc=False)
    until_dt = _parse_window_ts(until, is_until=True, naive_as_utc=False)
    entries = _audit_filter_rows(all_entries, ip=ip, token=token, action=action,
                                 provider=provider, rule=rule, q=q, shadow=shadow,
                                 since_dt=since_dt, until_dt=until_dt,
                                 status=status or "")

    def _t_key(x):
        return str(x.get("time") or x.get("ts") or "").replace("T", " ")[:19]
    _sort = (sort or "").strip()
    if _sort == "provider":
        entries = sorted(entries, key=lambda x: (x.get("provider") or "", _t_key(x)), reverse=True)
    elif _sort == "rule":
        entries = sorted(entries, key=lambda x: (x.get("rule") or "", _t_key(x)), reverse=True)
    elif _sort == "type":
        entries = sorted(entries, key=lambda x: (x.get("type") or "", _t_key(x)), reverse=True)
    elif _sort == "time_asc":
        entries = sorted(entries, key=_t_key)
    else:
        entries = sorted(entries, key=_t_key, reverse=True)

    total_filtered = len(entries)
    total_pages = max(1, (total_filtered + limit - 1) // limit)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * limit
    end = start + limit
    page_entries = entries[start:end]

    total = total_filtered
    c_allow = sum(1 for e in entries if e.get("action") == "allow")
    c_route = sum(1 for e in entries if _islr(e.get("action")))
    c_block = sum(1 for e in entries if _entry_blocked(e))
    c_other = total - c_allow - c_route - c_block

    kpi = {"total": total, "c_allow": c_allow, "c_route": c_route,
           "c_block": c_block, "c_other": c_other, "source": "window"}
    _un_sqlable = any((x or "").strip() for x in (ip, token, provider, q, shadow, status))
    if not _un_sqlable:
        _c = store.count_mysql(since=since_dt, until=until_dt,
                               action=(action or "").strip() or None,
                               rule=(rule or "").strip() or None)
        if _c:
            kpi = {"total": _c["total"], "c_allow": _c["c_allow"], "c_route": _c["c_route"],
                   "c_block": _c["c_block"],
                   "c_other": _c["total"] - _c["c_allow"] - _c["c_route"] - _c["c_block"],
                   "source": "mysql"}
    def _plain(v):
        if v is None or isinstance(v, (str, int, float, bool)):
            return v
        return str(v)

    _keys = ("time", "type", "rule", "action", "provider", "local", "model",
             "requested_model", "downgraded_from", "filename", "text_preview", "client_ip", "token_masked", "key_name", "session_id", "l2", "status_code", "risk_score", "layer", "findings", "file_findings")

    def _entry_val(k, e):
        if k == "time":
            return _to_beijing(e.get(k))
        if k in ("findings", "file_findings"):
            # 热窗键名 findings，MySQL 行读取后叫 findings_json：归一后整对象返回。
            # file_findings 只在热窗（Redis）键里，MySQL 无此列 ⇒ 窗口查询行返回 None。
            if k == "findings":
                return e.get("findings") or e.get("findings_json")
            return e.get(k)
        if k == "l2":
            return e.get(k)
        return _plain(e.get(k))

    for _e in page_entries:
        if not _e.get("status_code"):
            _e["status_code"] = _entry_status(_e)
    return JSONResponse({
        "total": total, "page": page, "pages": total_pages, "limit": limit,
        "c_allow": c_allow, "c_route": c_route, "c_block": c_block,
        "c_other": c_other,
        "kpi": kpi,
        "entries": [{k: _entry_val(k, e) for k in _keys} for e in page_entries],
    })

@router.get("/admin/csv", dependencies=[Depends(_admin_auth)])
async def export_csv(
    _ip=None,
    since=None,
    until=None,
    action=None,
    rule=None,
    type=None,
    ip=None,
    token=None,
    q=None,
    shadow=None,
    status=None,
    source="auto",
    limit=5000,
):
    import csv, io, re
    _parse_window_ts = _m("_parse_window_ts")
    _enrich_audit_key_name = _m("_enrich_audit_key_name")
    _audit_filter_rows = _m("_audit_filter_rows")
    _entry_status = _m("_entry_status")
    store = get_audit_store()
    cols = ["time", "type", "rule", "action", "status_code", "provider", "local", "model", "requested_model",
            "downgraded_from", "filename", "text_preview", "id", "client_ip", "token_masked", "key_name"]

    def _csv_safe(v):
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
            return "'" + v
        return v

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    if since or until:
        since_dt = _parse_window_ts(since, naive_as_utc=False)
        until_dt = _parse_window_ts(until, default=datetime.now(timezone.utc).replace(tzinfo=None), is_until=True, naive_as_utc=False)
        rows = store.query_mysql(since=since_dt, until=until_dt, action=action, rule=rule, type_=type, limit=limit)
        _enrich_audit_key_name(rows)
        rows = _audit_filter_rows(rows, ip=ip, token=token, q=q, shadow=shadow, status=status)
        for r in rows:
            if "error" in r:
                continue
            _row = {
                "time": _to_beijing(r.get("ts", "")),
                "type": r.get("type", ""),
                "rule": r.get("rule", ""),
                "action": r.get("action", ""),
                "status_code": r.get("status_code") or r.get("upstream_error") or _entry_status(r),
                "provider": r.get("provider", "") or "",
                "local": "1" if r.get("local_flag") else "0",
                "model": r.get("model", "") or "",
                "requested_model": r.get("requested_model", "") or "",
                "downgraded_from": r.get("downgraded_from", "") or "",
                "filename": r.get("filename", "") or "",
                "text_preview": (r.get("text_preview", "") or "")[:200],
                "id": r.get("req_id", ""),
                "client_ip": r.get("client_ip", "") or "",
                "token_masked": r.get("token_masked", "") or "",
                "key_name": r.get("key_name") or r.get("token_masked") or "",
            }
            w.writerow({k: _csv_safe(v) for k, v in _row.items()})

    else:
        rows_hot = sorted(store.all(), key=lambda x: str(x.get("time") or x.get("ts") or "").replace("T", " ")[:19])
        _enrich_audit_key_name(rows_hot)
        rows_hot = _audit_filter_rows(rows_hot, ip=ip, token=token, action=action,
                                      rule=rule, type_=type, q=q, shadow=shadow, status=status)
        for e in rows_hot:
            if not e.get("key_name"):
                e["key_name"] = e.get("token_masked") or ""
            if not e.get("status_code"):
                e["status_code"] = _entry_status(e)
            w.writerow({k: (_csv_safe(_to_beijing(e.get(k, ""))) if k == "time" else (_csv_safe(e.get(k, "")) if k != "local" else (1 if e.get(k) else 0))) for k in cols})

    from fastapi.responses import HTMLResponse
    return HTMLResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=gateway_audit.csv"})


@router.get("/admin/audit/status", dependencies=[Depends(_admin_auth)])
async def audit_status(_ip=None):
    from .main import admin_auth as _aa
    # keep auth semantics: call admin_auth lazily
    store = get_audit_store()
    _redact = lambda u: __import__("re").sub(r"://[^@/]*@", "://***@", str(u or ""))
    return {
        "backend": _redact(store.backend()),
        "redis": {
            "connected": store._r is not None,
            "url": _redact(store._r_url),
        },
        "mysql": store.mysql_status(),
    }
