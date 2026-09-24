"""laya-l2：L2 机密判定服务（Laya multilingual 非自回归决策模型，端口 8003，GPU 5）

Laya 不是生成式 LLM（无 OpenAI 协议，vLLM 跑不了），走 Python SDK + 薄 wrapper。
网关 src/gateway/small_model.py 的 laya 后端调 POST /classify，请求/响应都是
小 JSON（无 token 生成）。

判据从网关内置 L2 system prompt（small_model.SYSTEM_PROMPT）搬进 choice 题
criteria——换 Laya 不改判密口径。

不是 vllm-* unit：禁止通配批量操作（见 agents/vllm-hosts.md 硬约束）。
"""
import os
import time

os.environ.setdefault("USE_TF", "0")  # transformers 探测 TF 时 abseil 运行时可能死锁（官方 README 警告）

import laya
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

MODEL_PATH = os.getenv("LAYA_MODEL_PATH", "")  # 本地目录；空 = 从 HF 拉取
MODEL_ID = "convaiinnovations/laya"
SUBFOLDER = "multilingual"  # 根目录 checkpoint 只认英文，非拉丁脚本"准确率0@置信度0.95"，必须用 multilingual
PORT = int(os.getenv("LAYA_PORT", "8003"))

# noul + 英文题是 2026-09-22 探针实测的最优规格（8 组合对比）：
# 中文 choice 题下工资表 p 仅 0.35（假阴性）；noul 英文题下 5 类机密样本 0.76-1.0 全中，
# 但普通中文技术文档会误高（0.84-0.91）——误报率是影子期要测的主指标。
QUESTIONS = {
    "is_confidential": {
        "type": "noul",
        "instructions": (
            "Is this content enterprise-confidential (engineering drawings, financial data like "
            "salaries/costs/revenue, personal sensitive data like ID/phone/bank card, or business "
            "secrets like contracts/bids/internal docs)? A table with employee names and salary "
            "amounts is confidential."
        ),
        "criteria": {
            "true": "contains confidential content",
            "false": "public or everyday content",
        },
    },
}

app = FastAPI()
agent = None
_load_err = ""


@app.on_event("startup")
def _load():
    global agent, _load_err
    try:
        # 本地目录即模型目录（不再叠 subfolder）；空 = 从 HF 拉取
        agent = laya.load(MODEL_PATH) if MODEL_PATH else laya.load(MODEL_ID, subfolder=SUBFOLDER)
        # 1200 字中文预览 > 默认 768 token state 预算（max_len 1024 - head 256）；
        # mmBERT RoPE 支持到 8192，放宽到 2048 防尾部截断丢判据
        agent.cfg["max_len"] = 2048
    except Exception as e:
        _load_err = repr(e)
        raise


class Req(BaseModel):
    text: str = ""
    filename: str = ""
    headers: list = []
    sheet_names: list = []


def _render_state(r: Req) -> str:
    # 与 small_model.USER_PROMPT_TEMPLATE 同字段布局；**空字段不渲染**（与训练/评测
    # 数据布局一致）——实测空 `文件名: \nSheet: \n表头: \n` 前缀会把 v8 的 CAP 定理
    # 从 0.013 抬到 0.96（空前缀伪影，灰区误报放大器）
    lines = []
    if r.filename:
        lines.append(f"文件名: {r.filename}")
    if r.sheet_names:
        lines.append(f"Sheet: {', '.join(r.sheet_names)}")
    if r.headers:
        lines.append(f"表头: {', '.join(r.headers)}")
    if lines:
        lines.append("")
    lines.append("内容预览:")
    lines.append(r.text or "")
    return "\n".join(lines)


@app.post("/classify")
def classify(r: Req):
    if agent is None:
        return JSONResponse({"error": _load_err or "model not loaded"}, status_code=503)
    t0 = time.time()
    res = agent.predict(_render_state(r), QUESTIONS)
    a = res["answers"]["is_confidential"]
    p = float(a.get("noul", 0.0))  # noul: p[1] = "true/机密" 概率
    label = "CONFIDENTIAL" if p >= 0.5 else "NORMAL"
    probs = {"CONFIDENTIAL": p, "NORMAL": round(1 - p, 4)}
    # 阈值口径用「所选 label 的原始概率」（与 qwen 后端口径一致）
    conf = p if label == "CONFIDENTIAL" else 1 - p
    return {
        "label": label,
        "confidence": conf,
        "probabilities": probs,
        "reason": f"laya-multilingual noul p_conf={p:.3f}",
        "latency_ms": int((time.time() - t0) * 1000),
    }


@app.get("/health")
def health():
    if agent is None:
        return JSONResponse({"ok": False, "error": _load_err or "loading"}, status_code=503)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
