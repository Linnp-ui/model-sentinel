"""测试隔离三件套（AGENTS.md 约定）：import main 之前先生效。

1. AI_GATEWAY_DEV_API_KEY —— 与用例硬编码 key 一致，否则全量 401；
2. OLLAMA_MOCK=true —— 内网 provider 走产品自带 mock，不打 GPU；
3. SMALL_MODEL_ENABLED=false —— L2 恒回 NORMAL（latency 0），不打内网 8002。

外网（deepseek/openai…）：生产配 on_missing_key=reject + gateway_only，
测试 env 无凭据本会 503；_mock_external 把 routing._mock_enabled 猴补丁为
恒 True，allow 全链路走产品自带 mock（内容为假，但 action/provider/
status 口径不变），仍然零真实出境。local provider 本就走 OLLAMA_MOCK，
补丁对它们是等价行为。
"""
import os

os.environ.setdefault("AI_GATEWAY_DEV_API_KEY", "pk_live_dev_changeme,sk_test_dummy0123456789abcdef0123456789abcdef01")
os.environ.setdefault("OLLAMA_MOCK", "true")
os.environ.setdefault("SMALL_MODEL_ENABLED", "false")
os.environ.setdefault("AI_GATEWAY_ADMIN_IP_ALLOWLIST", "127.0.0.1,::1,testclient")
# 规则5 first-seen 落盘隔离：全 session 共用内存 store，suggestions 会写 marks 文件，
# 指到系统临时目录，别污染工作区 var/（生产网关读同一文件）。
import tempfile
os.environ.setdefault("AI_GATEWAY_SUGGEST_RULE_SEEN_PATH",
                      os.path.join(tempfile.mkdtemp(prefix="gw-test-seen-"), "rule_first_seen.json"))
# TestClient 的 client.host 是字面量 "testclient"，命中不了 127. 豁免；把它写进
# admin allowlist 才能调 /admin/api/*（线上 127 豁免不受影响）。

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _mock_external(monkeypatch):
    import src.gateway.routing as _rt
    monkeypatch.setattr(_rt, "_mock_enabled", lambda *a, **k: True)


@pytest.fixture(scope="session", autouse=True)
def _register_test_keys():
    """鉴权收紧后（仅登记表放行）：把用例硬编码 key 登记进测试 store（memory 后端，不碰生产库）。"""
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    for key, name in [
        ("sk_test_dummy0123456789abcdef0123456789abcdef01", "pytest"),
        ("pk_live_dev_changeme", "pytest-dev"),
    ]:
        try:
            _, found, _ = store.find_key_entry(key)
            if not found:
                store.create_api_key(key, name, "", "pytest session fixture")
        except Exception:
            pass
