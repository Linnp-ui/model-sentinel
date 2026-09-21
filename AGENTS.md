# AGENTS — Secure Gateway

> 公司内网大模型请求统一出口：L1 规则(毫秒级) → L2 小模型语义(~230ms)，
> 通过发外网，不通过自动降级到本地模型（内容不出境）。

> **`.env` 不入库**（见 `.gitignore`，从 `.env.example` 复制后本地填写）：
> 禁止把密钥明文写进任何入库文件、脚本或日志；
> key 明文只许出现在客户端运行时内存 / admin UI 密码框输入瞬间。

## 分层文档（按需读）

| 文件 | 内容 | 什么时候读 |
|---|---|---|
| `agents/deploy.md` | 部署到生产：备份 → 传 → hash → ast 预检 → 重启 → MainPID 核对 | 推代码到生产前 |
| `agents/vllm-hosts.md` | 内网 vLLM 主机端口表 + 进程操作硬约束 | 动 vllm unit / GPU / 新加模型 |
| `agents/policy-routing.md` | policy.yaml / routing.yaml 语义、L2 门、ccswitch 接入、X-Session-ID 会话粘性 | 改策略 / 路由 / 客户端接入 |
| `agents/env.md` | 关键 Env 变量 + 生产收口清单 | 动 env / 生产加固 |
| `agents/gotchas.md` | 坑位全记录（红队发现 / 事故 / 边界） | 动 inspection / file_inspector / audit / metrics / 部署前扫一遍 |

## 仓库编码

Linux 纯 UTF-8 + LF，`src/gateway/*.py` 与 `tests/*.py` 直接 `Read` / 编辑，
无打包头、无乱码。改完 .py 快速烟测：

```bash
.venv/bin/python -c "import ast; ast.parse(open('src/gateway/main.py', encoding='utf-8').read())"
```

## Quick Start

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt   # pytesseract 已在内（配系统 tesseract + chi_sim 即真 OCR）
.venv/bin/python -m pip install pytest reportlab       # 不在 requirements，测试用
cp .env.example .env                                   # AI_GATEWAY_DEV_API_KEY 必填

# 全量测试（conftest 三件套隔离 + 外部 HTTP 走产品 mock，零真实出境）
.venv/bin/python -m pytest tests/ -q                    # 当前基线：71 passed + 1 skipped（2026-09-20 重建 conftest 后）
.venv/bin/python -m pytest tests/test_gateway.py -k file_financial -v
```

> **改测试不要破坏 `tests/conftest.py` 的隔离**：DEV key + `OLLAMA_MOCK=true` +
> `SMALL_MODEL_ENABLED=false` + `routing._mock_enabled` 猴补丁恒 True。
> 删补丁/改回黑洞会让 allow 链路真打外网（503）并可能误触熔断。

## Run

```bash
# 离线（mock 内网）
.venv/bin/python -m uvicorn src.gateway.main:app --host 0.0.0.0 --port 8080

# 完整栈
docker compose up -d gateway postgres redis
docker compose --profile ollama up -d               # 含本地 ollama，需 GPU；现在用内网 vLLM 不必起
```

> 生产部署走 systemd（见 `agents/deploy.md`）：改 `src/*.py` 后
> `sudo systemctl restart gateway.service` + MainPID 变更核对 + `/health` 冒烟。
> 前端 `static/admin/` 磁盘直服，`web/admin` 里 `npm run build` 即生效，无需重启。

## 鉴权（旧"已关闭/BYOK"说法已过期，以本节为准）

- `/v1/*` 必带 key（`Bearer` / `x-api-key` 两头都认），无 key 401；黑名单 key 403（deny-overrides）；**未登记 key 直接 401**（2026-09-20 收紧：env dev key / 上游验签 fail-open 兜底已删，合法 key 先登记进表）。
- 外发吃公司额度**仅限具名登记身份**（admin 登记的 key）：未登记身份连本地路由也进不去（`auth()` 直接 401，不再放行到 `validate_environment` 判 503）。
- 生产 `AI_GATEWAY_UPSTREAM_KEY_MODE=gateway_only`；客户端自带 `X-Upstream-Api-Key` 直接 400。

## L1/L2 门（`main._review_text`，三条件与）

L1 allow **且**（实际喂模型的文本 >30 字 **或** `risk_score ∈ [20,60)` 灰区）**且**
（机密会话 **或** 非白名单豁免）→ L2；白名单 key 95% 跳 L2（5% 抽样）；
`/v1/embeddings` 永不 L2；灰区下限 env `AI_GATEWAY_L2_GRAY_FLOOR`（默认 20）。

risk 档（`inspection.py`）：≥150 block；≥60 route_local；
[20,60) 强制 L2；<20 直放。关键词三档 30/25/20，单命中 20 即进灰区。

延迟口径：P50/P95/P99 只算**网关自身** `gateway_internal_ms`
（`request_log` 列，`duration = gi + upstream`；ring 每 10s tick 取近 5 分钟窗口）。
典型值：L1 直判 1–3ms，+L2 ~230ms，上游生成秒级。

## Architecture（一行一个文件 = 它的真角色）

| 文件 | 关键职责 |
|---|---|
| `src/gateway/main.py` | FastAPI 入口；所有路由 + 鉴权（强制 401，见上节）+ 审计 + L1/L2 调度 + 流式降级 + 推理透传；响应码捕获中间件（写 ContextVar → `gateway_requests_v2_total{status_code}`）。`/v1/*` 端点一律 `Depends(_bind_identity_ctx)`（async 上下文），**不要**用裸 `Depends(auth)`，否则准入身份丢失、全量 503 |
| `src/gateway/policy.py` | YAML 策略引擎；**Pydantic 强校验**（`Policy` / `PolicyDoc`），错 action / 越界 priority / 缺 name 启动即抛 `ValidationError`；`decide()` 返回的 Policy 支持 `r["action"]` 字典式访问（routing.py 旧调用 0 改动） |
| `src/gateway/redaction.py` | 审计密钥脱敏：`log_entry()` 写 audit 前 in-place 替换 `text_preview` / `l2.explanation` / `reason` 等，覆盖 AKIA / PEM / ghp_/gho_/ghr_/ghs_/ghu_ / sk-ant- / sk-(proj-) / AIza / xox[abposr]- / JWT / Authorization Bearer。**PII 不脱敏**（身份证/手机/银行卡 走 route_local，audit 留原文方便追责） |
| `src/gateway/providers.py` | provider 注册表；`${VAR}` / `${VAR:default}` 展开；按 mtime 热重载；密钥只从 env 取。**写回 routing.yaml 必须走 `load_raw()` / `save_providers()`**（raw 未展开），否则 `${VAR}` 被展开吞掉 |
| `src/gateway/provider_keys.py` | `gateway_only` 凭据池（P1 只读 env）：per-provider 取 key，未登记身份 / 无凭据一律空串 fail-closed，**不抛错**（让 `validate_environment` 统一 503） |
| `src/gateway/llms/` | provider 线协议适配层（仿 LiteLLM llms/）：`base.py` BaseLLM 抽象；`openai_compat.py` / `anthropic_llm.py` 各实现一种 api_mode；`registry.py` 登记 api_mode→handler；`bridges.py` 承接入站协议桥（Anthropic Messages / OpenAI Responses ↔ 标准 chat）。**新增上游协议 = 加一个 BaseLLM 子类 + registry 登记一行，routing.py 零改动** |
| `src/gateway/routing.py` | `resolve()` 三级覆盖 + chat/embeddings/stream 转发 + Anthropic/Responses 协议转换；**单例 `httpx.AsyncClient`**（新代码别再 `httpx.AsyncClient()` 每请求）；`upstream_key()` per-provider 取凭据（别名组跨 provider 混排先改 per-candidate 解析，见函数 docstring） |
| `src/gateway/inspection.py` | 文本层：Regex(身份证/手机/AKIA/私钥) + 熵 + Luhn(银行卡) + 关键词三档 + PII 加权评分（≥60 route_local，[20,60) 强制 L2）。pattern 跑 NFKC + 去分隔符 copy，**关键词跑原文** |
| `src/gateway/file_inspector.py` | 文件层：PDF/Excel/Word/**图片（含 heic/avif）** + pymupdf 兜底 + 扫描件 OCR（paddleocr→pytesseract chi_sim+eng，5页/150DPI/8000字）；图片走 `_ocr_pixmap`（PDF/图片共享） + 20MB size cap；**解析在 `run_in_threadpool`**（不要直接 `await`）；扩展名不可信，以 magic 嗅探为准 |
| `src/gateway/small_model.py` | L2 `Qwen3-4B-Instruct-2507`；`CONFIDENTIAL_THRESHOLD=0.7`；LRU 500/TTL600；5 块并发；客户端必须复用；`latency_ms==0` 一律视为无真实调用（disabled / 缓存 / 空输入），不入直方图 |
| `src/gateway/responses_filectx.py` | codex `/v1/responses` 附件提取：`input_file` 块 filename 必取，`file_data` 仅 csv/txt/tsv 解析表头（pdf/xlsx 只看文件名）；prompt 里 `Files mentioned by the user` 段也提文件名 |
| `src/gateway/circuit_breaker.py` | 三态熔断 CLOSED→OPEN→HALF_OPEN；provider 级与 `prov::model` 级两套 breaker；admin 可手动 open/closed；生产阈值 2/30s/10s（`.env`，**不是**代码默认 5/60/30） |
| `src/gateway/metrics.py` | `/admin/metrics` Prometheus；**双指标**：`requests_total`（legacy 5 轴，dashboard 不破） + `requests_v2_total`（P2 7 轴 `{type,action,rule,provider,model,local,status_code}`，status 由中间件写 ContextVar 供） + override_denied_total / fallback_total / tokens_total / l2_latency_ms / circuit_* / ocr_* |
| `src/gateway/metrics_ring.py` | 运行指标环：10s tick 差分 → 24h 内存环 + JSONL 落盘（`var/metrics_ring.jsonl`，kill -9 不丢）；P99 取近 5 分钟 `request_log`（`AI_GATEWAY_PCT_WINDOW_S`） |
| `src/gateway/audit_store.py` | 审计双写：Redis 热缓存 `key=gateway:audit` `lpush+ltrim(0,4999)` + MySQL 持久化（后台线程批量写，fail-open，90 天清理） |
| `src/gateway/admin_api.py` | 管理面：45+ `/admin/api/*`（Basic/`/admin/login` cookie，会话 7 天）+ 运维三件套（`route-inspect` 干跑——文本外还支持 `channel=check/chat/responses` 文件干跑，codex∪workbuddy 并集、`providers/check` 健康、`circuit` 手动）+ provider 注册表 CRUD + provider key 配置（write-only，`env_file.py` 写 os.environ 即时生效 + 行级落盘）。**key 值永不回显**，ops-log 只记长度 |
| `src/gateway/rate_limit.py` | 令牌桶限流（in-memory + Redis 可选）；`extract_client_key` 按 token > X-Forwarded-For > IP（XFF 仅 `AI_GATEWAY_TRUST_XFF=true` 时采用，默认 socket IP 优先）；`check_model` 模型级全局 RPM（配置在 `model_policy.model_limits`，卡口在 `routing.resolve` 出口，ContextVar 按请求去重） |
| `src/gateway/session_store.py` | 会话级机密标记：`X-Session-ID` 下 files/check 文件命中、chat/messages 内联图或提及数据文件名命中（route_local/block）后同会话强制 route_local（TTL 见 .env，生产 7 天）；Redis + 内存兜底 |
| `src/gateway/user_models.py` | `/v1/models/register` 自助注册；`AI_GATEWAY_EGRESS_DOMAINS` 白名单；Redis `gateway:user_models` |

路由：`/v1/chat/completions`、`/v1/embeddings`、`/v1/messages`、`/v1/messages/count_tokens`、
`/v1/responses`、`/v1/files/check`、`/v1/models`、`/v1/models/register`、`/v1/{path:path}`
（兜底转发，生产开 `AI_GATEWAY_STRICT_EGRESS=true`）、`/admin*`、`/health`、静态 `/`。

客户端形态（测试有覆盖）：workbuddy = OpenAI 协议（`image_url` 内联图走 OCR）；
codex = `/v1/responses` `input_file` 块（见 `responses_filectx.py`）。
