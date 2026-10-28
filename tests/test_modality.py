"""模态感知选模：带图请求不落到纯文本候选。

背景：2026-10-27 实测 deepseek-v4-pro 收图返回 200 但 reasoning 自述
「图片不支持无法查看」（[Unsupported Image]）——静默降级比 400 更隐蔽。
Auto-AB 按复杂度档位选头，带图重任务会落到 3 档 v4-pro ⇒ 图片不可见。
修复：model_policy.yaml 候选声明 input 模态，选头/故障切换硬过滤。

运行: .venv/bin/python -m pytest tests/test_modality.py -v
"""
import base64
import io

import pytest
from fastapi.testclient import TestClient

from src.gateway import alias_router
from src.gateway.admin_store import get_admin_store
from src.gateway.main import app
from src.gateway.routing import RoutingError

client = TestClient(app)
KEY = "sk_shGD8A0lZpIJbcGavN8z6GBcSl-DGIsecJqgF4r0k_o"
H = {"Authorization": f"Bearer {KEY}"}

# 与 model_policy.yaml 现网声明对齐
C_QWEN = {"provider": "vllm_local", "model": "qwen2.5:7b", "tier": 1}
C_FLASH = {"provider": "deepseek", "model": "deepseek-flash", "tier": 2}
C_PRO = {"provider": "deepseek", "model": "deepseek-v4-pro", "tier": 3}


@pytest.fixture(autouse=True)
def _clean_request_ctx():
    """熔断器是进程级单例：前面测试记的上游失败可能把候选熔断打开，
    盖住本文件的选头断言（全量顺序下 test_gateway 后必现）。
    （intent 档位泄漏的根因在 conftest._no_production_l2_overrides，此处不复处理。）"""
    from src.gateway.circuit_breaker import get_breaker
    get_breaker().reset()
    alias_router.ctx_set_needs_image(False)
    yield


def _group(name, cands):
    return {"name": name, "candidates": list(cands), "intent": False}


def _png_data_url(draw_text="A1"):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (400, 100), "white")
    ImageDraw.Draw(img).text((20, 30), draw_text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------- 模态声明查询 ----------

def test_model_inputs_declarations():
    from src.gateway.model_policy import model_inputs
    assert "image" in model_inputs("vllm_local", "qwen2.5:7b")
    assert "image" in model_inputs("deepseek", "deepseek-flash")
    assert model_inputs("deepseek", "deepseek-v4-pro") == ["text"]
    assert model_inputs("nope", "nothing") == ["text"]  # 未声明=保守仅文本


# ---------- 单测：alias_candidates 模态过滤 ----------

def test_alias_candidates_no_image_keeps_all():
    out = alias_router.alias_candidates(_group("Auto-AB", [C_QWEN, C_FLASH, C_PRO]))
    assert {(c["provider"], c["model"]) for c in out} == {
        ("vllm_local", "qwen2.5:7b"), ("deepseek", "deepseek-flash"), ("deepseek", "deepseek-v4-pro")}


def test_alias_candidates_image_filters_text_only():
    alias_router.ctx_set_needs_image(True)
    try:
        out = alias_router.alias_candidates(_group("Auto-AB", [C_QWEN, C_FLASH, C_PRO]))
    finally:
        alias_router.ctx_set_needs_image(False)
    got = {(c["provider"], c["model"]) for c in out}
    assert ("deepseek", "deepseek-v4-pro") not in got
    assert ("vllm_local", "qwen2.5:7b") in got and ("deepseek", "deepseek-flash") in got


def test_alias_candidates_image_all_filtered_raises_400():
    alias_router.ctx_set_needs_image(True)
    try:
        with pytest.raises(RoutingError) as ei:
            alias_router.alias_candidates(_group("Auto-AB", [C_PRO]))
        assert ei.value.status_code == 400
        assert ei.value.type_ == "model_no_image"
    finally:
        alias_router.ctx_set_needs_image(False)


# ---------- 集成：chat 端到端（mock 上游） ----------

def _seed_alias(monkeypatch, cands):
    store = get_admin_store()
    monkeypatch.setattr(store, "get_alias_routes",
                        lambda: {"modtest": _group("ModTest", cands)})


def test_chat_image_lands_on_image_capable(monkeypatch):
    """带图请求：链首纯文本 v4-pro 被滤掉 ⇒ 落 deepseek-flash"""
    _seed_alias(monkeypatch, [C_PRO, C_FLASH])
    body = {"model": "ModTest", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "今天天气怎么样"},
        {"type": "image_url", "image_url": {"url": _png_data_url("A1")}}]}]}
    r = client.post("/v1/chat/completions", headers=H, json=body)
    assert r.status_code == 200
    assert r.json()["model"] == "deepseek-flash"


def test_chat_text_only_keeps_original_head(monkeypatch):
    """纯文本请求：不走模态过滤，链首仍是 v4-pro（无图行为零变化）"""
    _seed_alias(monkeypatch, [C_PRO, C_FLASH])
    body = {"model": "ModTest", "messages": [{"role": "user", "content": "今天天气怎么样"}]}
    r = client.post("/v1/chat/completions", headers=H, json=body)
    assert r.status_code == 200
    assert r.json()["model"] == "deepseek-v4-pro"


def test_chat_image_all_text_group_400(monkeypatch):
    """带图请求 + 全组纯文本候选 ⇒ 400 明示，不静默降级"""
    _seed_alias(monkeypatch, [C_PRO])
    body = {"model": "ModTest", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "今天天气怎么样"},
        {"type": "image_url", "image_url": {"url": _png_data_url("A1")}}]}]}
    r = client.post("/v1/chat/completions", headers=H, json=body)
    assert r.status_code == 400
    assert r.json()["detail"]["type"] == "model_no_image"
