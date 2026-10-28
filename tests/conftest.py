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

os.environ.setdefault("AI_GATEWAY_DEV_API_KEY", "pk_live_dev_changeme,sk_shGD8A0lZpIJbcGavN8z6GBcSl-DGIsecJqgF4r0k_o")
os.environ.setdefault("OLLAMA_MOCK", "true")
os.environ.setdefault("SMALL_MODEL_ENABLED", "false")
os.environ.setdefault("AI_GATEWAY_ADMIN_IP_ALLOWLIST", "127.0.0.1,::1,testclient")
# 4. 明细/审计后端强制 memory —— 测试 seed 绝不进生产库。
#    事故记录（2026-09-29）：stats 用例把 7 行 ts=2030-06-01 的 pytest-chart 种子
#    写进了 gateway_admin.request_log，而统计窗是 `ts >= since` 无上界 ⇒ 管理台明细/
#    图表顶部天天出现 2030 的行。env 必须在这里清空（conftest 先于测试模块 import，
#    store 单例初始化晚于此），空值让 admin_store/audit_store 各自落 memory 后端。
for _v in ("ADMIN_DB_URL", "DATABASE_URL", "AUDIT_MYSQL_URL", "AUDIT_DATABASE_URL", "REDIS_URL"):
    os.environ[_v] = ""
# 5. l2 持久化覆盖指向空临时文件——生产 l2_overrides.yaml 是共用真文件
#    （SMALL_MODEL_ENABLED: true 等），app startup 时 apply_overrides() 会把它回灌
#    进 env，盖掉上面 setdefault 的测试值 ⇒ 测试期间 intent/L2 真打内网 8002
#    （非确定性选头 + 真实流量出测试域）。2026-10-27 由 test_modality 选头断言
#    暴露（真判 level 改写候选顺序）。指向空文件而非 no-op：保留 apply 机制
#    （test_l2_retrain_ensure_overrides 会自己 setenv 换回自己的临时文件）。
import tempfile as _tempfile
_ov_empty = _tempfile.mktemp(prefix="gw-test-l2ov-", suffix=".yaml")
open(_ov_empty, "w").close()
os.environ["AI_GATEWAY_L2_OVERRIDES_PATH"] = _ov_empty
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
        ("sk_shGD8A0lZpIJbcGavN8z6GBcSl-DGIsecJqgF4r0k_o", "pytest"),
        ("pk_live_dev_changeme", "pytest-dev"),
    ]:
        try:
            _, found, _ = store.find_key_entry(key)
            if not found:
                store.create_api_key(key, name, "", "pytest session fixture")
        except Exception:
            pass
