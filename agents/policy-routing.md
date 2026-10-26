# Policy & Routing / 客户端接入

> 上级：`AGENTS.md`。

## Policy & Routing

- `policy.yaml` priority 升序（**命中即停**）：3 block_secrets（密钥/私钥，block）>
  5 tainted_file_ref（正文提及已判密文件名）> 10 financial_local_only（财务表格关键词）>
  12 session_route_local（会话已判密）> 13 unparsed_binary_route_local（嗅探/解析失败
  fail-closed）> 20 pii_weighted_route_local（PII 加权 >= 60）>
  30 ocr_empty_image_route_local（OCR 空结果 fail-closed）> 40 drawing_local_only >
  100 default_allow。L2 命中不记 policy 规则名，记 `rule=l2:<scope>:<原因>`
  （如 `l2:last_user:含工资数据`）
- `routing.yaml` 决定"具体端点"：`routing.default_external` / `default_local` /
  `default_embedding` / `on_block`（默认 `fallback_local` 自动降本地；改 `reject` 才 403） /
  `on_missing_key`（`mock` 或 `reject`）
- **安全底线**：`action` 为 `route_local`/`block` 时强制走 `local: true` provider；
  客户端用 `model` 前缀（如 `openrouter/deepseek-v4-flash`）或 `X-Gateway-Provider` /
  `X-Gateway-Model` 头指定外网一律拒绝并记 `gateway.override_denied`
- 覆盖优先级（仅 `allow` 时生效）：`policy.target` < `model` 前缀 < `X-Gateway-Provider/Model` 头
- **对外模型名**：典型部署 `/v1/models` 暴露 `Auto-AB`（别名组，按本轮任务复杂度
  1 轻 / 2 中 / 3 重 由 L2 小模型选头，会话级档位粘性只升不降）与 `local-model`
  （单候选直连本地，内容不出境）；`AI_GATEWAY_PUBLIC_MODELS` 白名单只收窄
  **可发现性**、不影响点名调用。**裸模型名**先查用户自注册，再查
  `model_policy.internal_models` 登记的内网 chat 模型（命中 ⇒ 钉回内网 provider，
  不出境）；非 chat 的判定模型 ⇒ 404 `model_not_chat`
- L2 触发（`main._review_text`）：L1 allow **且**（`AI_GATEWAY_L2_ALWAYS=true` **或**
  `risk_score >= 20`，灰区下限 env `AI_GATEWAY_L2_GRAY_FLOOR`，**无上界**）**且**
  （机密会话 **或** 非白名单豁免）（2026-09-23 起取消字数触发）。
  `AI_GATEWAY_L2_ALWAYS` 生产常开：risk=0 的语义泄密 L1 完全打不中，打开后所有 L1 放行
  请求都送 L2（+~230ms/请求，多块 ~400ms），route-inspect 同口径；
  白名单 key 95% 跳 L2（`AI_GATEWAY_WL_L2_SAMPLE=0.05`，5% 抽样）；
  `/v1/embeddings` 同样走 `_review_text`（与 chat 同源）。
  命中 CONFIDENTIAL 转本地（`route_local`，`rule=l2:…`）。
  `POST /admin/api/route-inspect` 可干跑整条链（口径与线上门一致，返回 `trigger: gray`）
- 热重载：`policy.load_policy()` 每次请求重载；`providers.load_routing()` 按 mtime 重载；
  `docker-compose.yml` 把两个 yaml 挂 `:ro`
- **Provider 命名**：`vllm_local` = 本地降级（`OLLAMA_BASE_URL` 后端是 vLLM）；env 名
  `OLLAMA_BASE_URL/MOCK/API_KEY` 保留（不是后端名）
- **推理透传**：Anthropic `body.thinking` → OpenAI `reasoning.effort + include_reasoning: true`；
  上游 `delta.reasoning` / `delta.reasoning_content` → Anthropic `thinking_delta` /
  Responses `response.reasoning_text.delta`

## ccswitch 接入（Claude Code / Codex CLI）

| 工具 | 字段 | 值 |
|---|---|---|
| Claude Code | `ANTHROPIC_BASE_URL` | `http://网关IP:8080`（**不带** `/v1`） |
| Claude Code | `ANTHROPIC_AUTH_TOKEN` | 网关 key（必填，无 key 401） |
| Codex | `Base URL` | `http://网关IP:8080/v1`；`wire_api=responses` 或 `chat` 都行 |

- 鉴权 `Bearer` / `x-api-key` 两头都认，无 key 401；黑名单 403（deny-overrides）；
  未登记 key 401（2026-09-20 收紧，无兜底）。详见 `AGENTS.md` 鉴权节
- 协议转换在 `routing.py`：Anthropic `stop_reason` 映射（`stop→end_turn`, `length→max_tokens`）；
  `/v1/responses` 上游 404 时自动回退 chat 转换
- `claude-*` 模型名会被策略 target 覆盖；外网具体模型用 `X-Gateway-Model` 头

## 文件名污染 taint（客户端无感的会话替代，2026-09-23 起）

客户端不发会话头也能防上下文绕过：记"被污染的文件名"而非"会话"。files/check
命中 / chat 文本因含数据文件名被判 route_local/block 时，记
`(key 指纹, 小写文件名)`（`session_store` 按 key 存集合，TTL 同 session）；
chat/messages/responses 正文提及库内文件名即 `tainted_file_ref`（priority 5，
排在财务/会话规则之前）走本地。mention 提取常带中文前缀（"帮我看看X.xlsx"整体命中），
读写都只比后缀（任一方向 endswith 即中，保守方向）。codex/ workbuddy/ 新客户端
零改动自动覆盖；局限：改名即认不出（内容重贴仍会被 L1/L2 当场命中）。

## X-Session-ID 会话级机密（防上下文绕过）

上传机密文件后，同会话后续请求即使**不含触发关键词**也强制走本地模型：

```
请求 1: POST /v1/files/check   + X-Session-ID: sess-abc123  (上传工资表)
        → financial_local_only → route_local，同时给 session 打机密标（TTL 30min）
请求 2: POST /v1/chat/completions + X-Session-ID: sess-abc123  "帮我看看那份文件"
        → 无关键词，但 session.confidential=true → session_route_local (priority 12) → route_local
```

- 客户端只需在所有请求带同一个 `X-Session-ID`（或 `X-Gateway-Session-ID` /
  `X-Conversation-ID`）；ID 格式：8-128 位 `[A-Za-z0-9_-.]`
- **codex 不发会话头**（实测 2026-09-20：887/887 请求无 session 头，也不发
  `previous_response_id`，每轮全量重发 input）：`/v1/responses` 回落
  `main._codex_session_id`——对话首条 user 消息（environment_context+首问）的
  sha256 前 16 位作 session key（`codex-<hash>`）。codex 每轮重发全量 input，
  机密内容每轮都会被 L1/L2 重新命中（主保障）；指纹是粘性标记 + 审计按会话归组
- 打标触发（命中 `route_local/block`，含 L2 判定）：
  - `/v1/files/check`：文件命中即标
  - `/v1/responses`：附件/内联文件参与才标（纯文本命中不标，`responses_should_mark`）
  - `/v1/chat` / `/v1/messages`（`main._mark_session_hit`）：内联图片直接标；
    纯文本需正文提及数据文件名（`_mentioned_data_filename`：csv/tsv/txt/xlsx/xls/
    pdf/docx 后缀）才标——workbuddy 发 CSV 是「文件路径：…+ 全文粘贴」无字节附件，
    只有文件名可依赖；纯闲聊（无文件名）不标
- 清除：TTL 到期自动清（`AI_GATEWAY_SESSION_TTL`，**生产 .env 为 604800=7 天**，
  代码默认 1800s；误标成本按 7 天算，考虑调短）
- 审计：`gateway:audit` 与 MySQL `audit_logs.session_id` 都记录会话 ID，可事后按会话追溯

> 2026-09-18 起 client 身份绑定标记（换 session / 不带 session 头也能跟住的那层）
> 已整体移除，只剩 session 级；背景见 `gotchas.md` P1 红队条目。
