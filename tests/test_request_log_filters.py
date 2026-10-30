"""request_log 明细分页的精确过滤回归：key_name / ip / model / action / provider / status_code。

内存后端是进程内单例（AdminStore.__new__ 缓存 _instance），整条 test session 共享同一份
_logs——所以字段过滤的断言一律叠加唯一的 key_name 限定到本用例自插的行，
避免被其它测试写入的行（如 status_code=403）串扰，顺序无关。
"""
from src.gateway.admin_store import get_admin_store

KA, KB = "flt-a", "flt-b"


def _seed(store, **over):
    row = {"key_name": KA, "model": "m1", "provider": "provA",
           "action": "allow", "status_code": 200, "client_ip": "10.0.0.1"}
    row.update(over)
    store.insert_request_log(row)


def test_list_request_logs_exact_filters():
    s = get_admin_store()
    _seed(s)                                                                    # A: provA/200/allow/10.0.0.1
    _seed(s, key_name=KB, provider="provB", action="route_local", client_ip="10.0.0.2")  # B
    _seed(s, model="m2", action="block", status_code=403, client_ip="10.0.0.3")  # C: 同为 flt-a

    q = lambda **kw: s.list_request_logs(limit=100, key_name=KA, **kw)
    assert q()["total"] == 2                       # A + C
    assert s.list_request_logs(limit=100, key_name=KB)["total"] == 1  # B
    # 各字段过滤（限定 flt-a 内 A vs C）
    assert q(status_code=403)["total"] == 1        # C
    assert q(status_code=200)["total"] == 1        # A
    assert q(ip="10.0.0.3")["total"] == 1          # C
    assert q(action="block")["total"] == 1         # C
    assert q(action="allow")["total"] == 1         # A
    assert q(model="m2")["total"] == 1             # C
    # provider 在 flt-a 内全是 provA：负断言 + 换 key 正断言
    assert q(provider="provB")["total"] == 0
    assert s.list_request_logs(limit=100, key_name=KB, provider="provB")["total"] == 1  # B
    # 多字段组合
    assert q(provider="provA", status_code=403)["total"] == 1   # C
    assert q(provider="provB", status_code=403)["total"] == 0   # 无解


def test_request_log_facets():
    s = get_admin_store()
    _seed(s)
    f = s.request_log_facets()
    # 自插值必须在对应下拉里；空值（本用例未插空行也覆盖他人数据）不得出现
    assert KA in f["key_name"] and KB in f["key_name"]
    assert "m2" in f["model"]
    assert "provA" in f["provider"] and "provB" in f["provider"]
    assert "10.0.0.3" in f["client_ip"]
    for k, vals in f.items():
        assert "" not in vals and vals == sorted(vals), k
