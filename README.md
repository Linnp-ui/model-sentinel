# Secure Gateway

> 大模型请求统一出口：机密内容（图纸、财务表格、PII）自动转内网模型、内容不出境；
> 普通请求放行外网 provider。客户端只需改 `base_url` + `api_key`，SDK 代码不动。

## 快速开始

```bash
# Docker
cp .env.example .env   # 填 provider key / 内网地址 / AI_GATEWAY_ADMIN_PASSWORD
docker compose up -d gateway redis

# 本地裸跑（mock 内网，零外发）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # OLLAMA_MOCK=true
.venv/bin/python -m uvicorn src.gateway.main:app --host 0.0.0.0 --port 8080
```

打开 `http://localhost:8080/admin`，用 `admin` + 管理密码登录，「Key」页登记一个网关 key（未登记 key 调 `/v1/*` 返回 401）。

## 接入

```bash
export OPENAI_BASE_URL=http://<网关>:8080/v1
export OPENAI_API_KEY=<你的网关 key>
```

- 对话模型：`Auto-AB`（按任务复杂度自动选档）或 `local-model`（强制内网）
- 端点：`/v1/chat/completions`、`/v1/messages`、`/v1/responses`、`/v1/embeddings`
- 文件预检：`POST /v1/files/check`（multipart `file`）

## 示例

```bash
# 普通对话 -> 放行外网
curl -s $OPENAI_BASE_URL/chat/completions -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"Auto-AB","messages":[{"role":"user","content":"总结 CAP 定理"}]}'

# 财务表格 -> 自动转内网
curl -s http://<网关>:8080/v1/files/check -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F "file=@工资表.xlsx"
```

响应里 `gateway.action` 为 `allow`（外网）或 `route_local`（内网），`policy_rule` 说明命中规则。

## HTTPS

网关默认 HTTPS（端口 8080，自签内网 CA）。装 CA 一次即可免告警：见 `docs/https-cert-install.md`；证书下载 `/ca.crt`。

## 更多

- 用户接入细节：`docs/user-manual.md`
- 管理台操作：`docs/admin-manual.md`
- 策略/路由/环境变量：`agents/` 目录

## 测试

```bash
.venv/bin/python -m pytest tests/ -q   # 零真实出境（mock 隔离）
```
