# Env 与生产收口

> 上级：`AGENTS.md`。

## Env（关键变量）

- 必填：`AI_GATEWAY_DEV_API_KEY`（逗号分隔多 key；**仅进门**，外发额度只认 admin 登记的具名 key，
  dev key 外发 503 fail-closed）
- 内网：`OLLAMA_BASE_URL`（:8000）、`BGE_BASE_URL`（:8001）、`SMALL_MODEL_URL`（:8002）、
  `SMALL_MODEL_ENABLED`（默认 `true`；测试设 `false` 关闭 L2）
- 外网（可空 + `OLLAMA_MOCK=true` 走 mock）：`OPENAI_API_KEY` / `DEEPSEEK_API_KEY` /
  `DASHSCOPE_API_KEY` / `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY`。
  **systemd `EnvironmentFile` 无行内注释**：`KEY=sk-x # 备注` 整行都是值，
  key 会被污染（曾真实发生）；注释必须另起一行
- 白名单：`AI_GATEWAY_EGRESS_DOMAINS`（用户注册）、`AI_GATEWAY_EGRESS_PATHS`（兜底转发）
- 安全：`AI_GATEWAY_STRICT_EGRESS=true`（兜底路径白名单外一律 403，**生产必开**）、
  `AI_GATEWAY_ADMIN_IP_ALLOWLIST`（`/admin*` 的客户端 IP 白名单，私有网段恒放行）
- 调参：`AI_GATEWAY_MAX_TOKENS_CAP` / `AI_GATEWAY_RATE_LIMIT_RPM` / `RPS` / `FILES_RPM`
  （0=不限；429 响应 `Retry-After`）
- 会话：`AI_GATEWAY_SESSION_TTL`（默认 1800s）
- 审计：`AUDIT_MYSQL_URL`（生产 `mysql://root:changeme@127.0.0.1:3306/gateway_audit`，
  示例，复用现有 docker MySQL 即可）
- 熔断：`AI_GATEWAY_CB_FAILURE_THRESHOLD` / `WINDOW_SECONDS` / `RECOVERY_SECONDS` /
  `SUCCESS_THRESHOLD`（生产 2/30s/10s/2，见 `.env`；代码默认 5/60/30/2）
- L2 门：`AI_GATEWAY_L2_GRAY_FLOOR`（默认 20）、`AI_GATEWAY_WL_L2_SAMPLE`（默认 0.05）
- 指标：`AI_GATEWAY_RING_TICK_S`（默认 10）、`AI_GATEWAY_PCT_WINDOW_S`（默认 300）
- 明细库：`ADMIN_DB_URL` 优先，回退 `DATABASE_URL`（无则内存后端，重启即丢；
  shell 里直连须先 `export` 这两个变量，`.env` 不会自动加载）
- 上游 key 档：`AI_GATEWAY_MODELS_LIST_KEY_<PROVIDER>`（拉模型列表/健康检查专用，不参与真实转发）
- 配置路径：`AI_GATEWAY_POLICY_PATH` / `AI_GATEWAY_ROUTING_PATH`

## 收口（生产环境必须做）

1. 客户端只改 `base_url=http://网关:8080/v1` + `api_key=<网关key>`，SDK 代码不动
2. **网络层配合**：防火墙只放行网关访问外网大模型端点；办公网到外网直连全封
3. 审计走 `/admin` 与 `/admin/csv`；Prometheus 走 `/admin/metrics`（v0.3.0 起改查
   `gateway_requests_v2_total` 7 轴，旧的 5 轴保留不破 dashboard）；每次响应 `gateway` 字段
   含 `provider / endpoint / local / downgraded_from / override_denied`
