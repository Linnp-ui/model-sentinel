"""provider 探活补「影子 L2」一项（laya 8003）的回归用例。

背景：v35 残缺权重致 laya 崩溃循环 15.5h 而 /health 全绿——本模块的 probe_shadow
负责把 8003 的存活并进 /health。这里只测纯函数（不打循环、不打外网）。
"""
import src.gateway.provider_probe as pp


class _Resp:
    def __init__(self, code, payload):
        self.status_code = code
        self._p = payload

    def json(self):
        return self._p


def test_probe_shadow_none_when_unset(monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_L2_SHADOW_URL", raising=False)
    assert pp.probe_shadow() is None


def test_probe_shadow_ok_hits_health_endpoint(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_L2_SHADOW_URL", "http://127.0.0.1:8003/classify")
    calls = {}

    def fake_get(url, **kw):
        calls["url"] = url
        return _Resp(200, {"ok": True, "model": "laya-finetuned-confidential-v34"})

    monkeypatch.setattr(pp.httpx, "get", fake_get)
    e = pp.probe_shadow()
    assert calls["url"] == "http://127.0.0.1:8003/health"
    assert e["ok"] is True
    assert e["model"] == "laya-finetuned-confidential-v34"
    assert e["local"] is True


def test_probe_shadow_down_reports_error(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_L2_SHADOW_URL", "http://127.0.0.1:8003/classify")

    def fake_get(url, **kw):
        raise ConnectionError("refused")

    monkeypatch.setattr(pp.httpx, "get", fake_get)
    e = pp.probe_shadow()
    assert e["ok"] is False
    assert "refused" in e["error"]
