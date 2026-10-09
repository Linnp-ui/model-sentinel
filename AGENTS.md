# AGENTS — Secure Gateway

> 公司内网大模型请求统一出口：L1 规则(毫秒级) → L2 小模型语义(~230ms)，
> 通过发外网，不通过自动降级到本地模型（内容不出境）。
>
> **密钥纪律**：key 明文只许出现在客户端运行时内存 / admin UI 密码框输入瞬间，
> 禁止写进任何入库文件、脚本或日志。`.env` 不入库（见 `.gitignore`，从 `.env.example`
> 复制后本地填写）；`routing.yaml` 只写 `api_key_env` 变量名。

## 分层文档（按需读）

| 文件 | 内容 | 什么时候读 |
|---|---|---|
| `agents/deploy.md` | 部署到生产：备份 → 传 → hash → ast 预检 → 重启 → MainPID 核对 | 推代码到生产前 |
| `agents/vllm-hosts.md` | 内网 vLLM 主机端口表 + 进程操作硬约束 | 动 vllm unit / GPU / 新加模型 |
| `agents/policy-routing.md` | policy.yaml / routing.yaml 语义、L2 门、ccswitch 接入、X-Session-ID 会话粘性 | 改策略 / 路由 / 客户端接入 |
| `agents/env.md` | 关键 Env 变量 + 生产收口清单 | 动 env / 生产加固 |
| `agents/gotchas.md` | 坑位全记录（红队发现 / 事故 / 边界）| 动 inspection / file_inspector / audit / metrics / **动任何 systemd unit 或判断服务是否在跑** / 部署前扫一遍 |

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
.venv/bin/python -m pip install pytest                # 不在 requirements，测试用
cp .env.example .env                                   # AI_GATEWAY_DEV_API_KEY 必填

# 全量测试（conftest 四件套隔离 + 外部 HTTP 走产品 mock，零真实出境）
.venv/bin/python -m pytest tests/ -q                    # 基线 132 passed + 1 skipped（2026-09-29，~5s）；数字仅参考，以全量实跑为准
.venv/bin/python -m pytest tests/test_gateway.py -k file_financial -v
```

> **跑测试前不要 `source .env`**：导出后 `AI_GATEWAY_ADMIN_PASSWORD` 生效，管理台用例
> 全要登录 → 约 40 个 401（改动前后一致的老坑）。要连生产库排查就单开一个 shell。
>
> **但运维脚本 / 直调 `admin_api` / 查审计必须 `source .env`**：URL 类 env 读不到时
> admin/audit store **静默落 memory 后端**，`tail()` 返回空、`query_mysql` 返回 `[]`，
> **不报错**（2026-09-29 回填灌数轮次时踩了：以为"没有历史轮次"，实际是没连上 Redis）。
> 判据：结果为空且没报错 → 先怀疑 env 没加载，再怀疑数据真没有。
>
> **改测试不要破坏 `tests/conftest.py` 的隔离**：DEV key + `OLLAMA_MOCK=true` +
> `SMALL_MODEL_ENABLED=false` + `routing._mock_enabled` 猴补丁恒 True
> + **清空 `ADMIN_DB_URL`/`DATABASE_URL`/`AUDIT_MYSQL_URL`/`AUDIT_DATABASE_URL`/`REDIS_URL`**
> （空值 → admin/audit store 落 memory 后端，2026-09-29 加；事故：此前 store 单例选中生产
> MySQL，380 行 pytest 种子留在 `request_log`（含 ts=2030 的 14 行，统计窗 `ts>=since` 无上界
> ⇒ 管理台顶部天天出现 2030），Redis `gateway:audit` 也被写脏）。
> 删 env 清空那几行 = 测试重新污染生产库；删前两项补丁会让 allow 链路真打外网（503）并可能误触熔断。
> 同 session 共用 memory 后端 store：seed 用唯一 key_name/label、Top1 类断言播足量行，
> 断言按建议 id/type 过滤（残留行会抢 Top 位，见 `agents/gotchas.md`）。

## Run

```bash
# 离线（mock 内网）
.venv/bin/python -m uvicorn src.gateway.main:app --host 0.0.0.0 --port 8080

# 完整栈（明细/审计库走 MySQL，见 agents/env.md；不配 DB 则内存后端）
docker compose up -d gateway redis
```

> 生产部署走 systemd（见 `agents/deploy.md`）：改 `src/*.py` 后需重启 `gateway.service`
> + MainPID 变更核对 + `/health` 冒烟。重启按 `agents/deploy.md` 走完整流程
> （备份 → 预检 → 重启 → PID 核对）。
> **判断服务活不活只认 `ss -ltnp` / `docker ps` + 直打一次**——vLLM 系 unit 命名不规律
> （一个 unit 统管多卡 TP），`systemctl list-units | grep` 不到 ≠ 没在跑（见 `agents/gotchas.md`）。
> 查杀进程用具体 PID，别用 `pkill -f '<串>'`
>（自身命令行含同串会连自己一起杀；临时评测服务更同 argv，**只能 `$!` 记 PID**）。前端 `static/admin/` 磁盘直服，
> `web/admin` 里 `npm run build` 即生效（vite `outDir` 直写 `../../static/admin`，无拷贝步骤），无需重启；
> **源码与 `static/admin` 必须同一提交**（`git add` 在仓库根执行，workdir 在 web/admin 会 pathspec 失败）。
> build 后核对 served 产物：index.html 由 `/admin/app` 路由吐（恒 `Cache-Control: no-cache`，
> 入口必须重验，否则旧 index-*.js 找已删旧 chunk → 404），**js/css 资源在
> `/static/admin/assets/*`**（`/admin/assets/*` 是 404）；页面级 lazy chunk 名在 index-*.js 里。
> 核对一条命令（entry 引用的 hashed chunk 必须都在磁盘，缺一个就是旧入口）：
> `S=$(curl -s 127.0.0.1:8080/admin/app | grep -oE 'src="[^"]*\.js"' | head -1 | sed 's/src="//;s/"//'); curl -s "127.0.0.1:8080$S" | grep -oE '[A-Za-z0-9]+-[A-Za-z0-9_-]{8}\.js' | sort -u | while read f; do [ -f static/admin/assets/$f ] || echo "缺产物 $f"; done`
> （`-{8}` 是 vite 哈希位，别放宽成 `[A-Za-z0-9_-]+\.js`：entry 里 `"min.js"` 这类字符串会误报）
> `npm run build` 先跑 eslint（`web/admin/.eslintrc.cjs`，`no-undef`/`no-unused-vars` error），lint 不过不出包。
> 管理台走 HTTP（局域网 IP 访问）= **非安全上下文**：无 `navigator.clipboard`，
> 复制类功能必须带 execCommand 兜底（KeysPage/AuditPage 已有范式）。

## 鉴权（旧"已关闭/BYOK"说法已过期，以本节为准）

- `/v1/*` 必带 key（`Bearer` / `x-api-key` 两头都认），无 key 401；黑名单 key 403（deny-overrides）；**未登记 key 直接 401**（2026-09-20 收紧：env dev key / 上游验签 fail-open 兜底已删，合法 key 先登记进表）。
- 外发吃公司额度**仅限具名登记身份**（admin 登记的 key）：未登记身份连本地路由也进不去（`auth()` 直接 401，不再放行到 `validate_environment` 判 503）。
- 生产 `AI_GATEWAY_UPSTREAM_KEY_MODE=gateway_only`；客户端自带 `X-Upstream-Api-Key` 直接 400。

## L1/L2 门（`main._review_text`，三条件与）

L1 allow **且** `risk_score >= 20`（下限 env `AI_GATEWAY_L2_GRAY_FLOOR`，**无上界**）**且**
（机密会话 **或** 非白名单豁免）→ L2（2026-09-23 起取消字数触发，只看风险分）；
白名单 key 95% 跳 L2（5% 抽样）；
`/v1/embeddings` 永不 L2。
**全本地别名组**（启用候选 provider 全内网，如 `local-model`，`main._all_local_alias`）
跳过 L1/L2 直接 allow：内容不可能出境（所有故障切换路径跳过 local 候选，全组失败 fail-loud）。

`risk_score` 全仓库只有**两个**消费方，别按"分档表"理解它：
① `policy.yaml` 里唯一按分值的规则判 `findings.weighted_pii_score >= 60`（**不含关键词分**）；
② 灰区 `_L2_GRAY_FLOOR(20) <= risk_score < 60` 强制 L2（`main._l2_gray_trigger`）。

**关键词命中会转内网，有三条路（别只记"关键词不管用"）**：
- **A. `financial_local_only`（priority 10）直接命中**：`policy.yaml` 里
  `text contains_any: [财务报表, 资产负债, 利润表]`，纯关键词、与风险分无关。
- **B. 灰区 → L2 判密（主力路径）**：单个 20 档词 kw=20 恰在灰区下界，L2 触发判
  CONFIDENTIAL 即转本地。实测「工资表里的数据」kw=20、「机密」kw=30 都走这条路。
- **C. `weighted_pii_score>=60`（priority 5）**：只算 PII 权重，**不含关键词分**。

**当前态**：灰区**无上限**（`main._l2_gray_trigger` 只留下限）⇒ 关键词堆到
`risk_score>=60` 且 `weighted_pii_score=0` 也转内网、不再出境；回归
`test_l2_gray_trigger_no_upper_bound_on_risk`。暗语/零特征四条正则
（`implied_amount`/`raise_amount`/`salary_pinyin`/`exec_schedule`，各 20 档、归一化副本抗零宽，
**门逻辑没动**——20 档恰落灰区下界、判密仍归 L2）；回归
`test_l1_rules_catch_obfuscated_negative_samples`。历史（原缺口实测 / 版本矩阵 / 漏网数）见 `agents/gotchas.md`。

block 只由 priority 20/25 两条正则规则决定，与分值无关
（`severity` / `RISK_THRESHOLD_BLOCK` 曾是死字段，2026-09-29 从未读过，
2026-10-09 已从 `inspection.py` 删除 —— 别再加回来）。
**灰区探针/压测只许 `vllm_local/qwen2.5:7b` 斜杠写法**：`local-model` 别名走
`local_alias_bypass` 跳过 L1/L2（测不到 L2）；`qwen2.5:7b` 裸名落 `default_external`
**出境**。
L2 判密 → `l2_confidential` route_local；L2 不可用 → `l2_unavailable` route_local
（`AI_GATEWAY_L2_FAIL_MODE=open` 则放行）。

延迟口径：P50/P95/P99 只算**网关自身** `gateway_internal_ms`
（`request_log` 列，`duration = gi + upstream`；ring 每 10s tick 取近 5 分钟窗口）。
典型值：L1 直判 1–3ms，+L2 ~230ms，上游生成秒级。

**别在这三处找优化（2026-09-29 实测）**：
- `main._review_text` 纯 L1 中位 **0.19–0.36ms**（inspect_text 0.013ms/短文、0.19ms/516字；
  decide 0.05ms；session/taint 各一次 Redis GET 0.07–0.12ms）。可并行的只有那两次 GET，
  `asyncio.gather`+threadpool 跳转成本 ≈ 0.3–0.5ms > 收益，且 decide 依赖 inspect 的 findings
  （拆开改语义）⇒ **L1 并行化上限 0.2ms，不做**。真要省那 0.19ms 用一个 Redis pipeline。
- 审计**列表**不查 MySQL：走 `audit_store.tail(5000)`（Redis `LRANGE`）+ 版本签名
  （`LLEN`+最新一条）解析缓存，冷 59ms / 热 9–15ms，翻页深度无差异（分页在最后切片）。
  **别去加 MySQL keyset 分页/索引**。硬上限 = 100 页 × 50 = 5000 条（可见性问题，非性能问题）。
  头部 **KPI 查审计库**：`count_mysql()` 单遍聚合（时间/动作/规则筛选），全文/调用方/IP/影子
  筛选回退热窗口径，`kpi.source` 区分；顶层 `total/c_*` 保持窗口口径供分页（2026-10-08 修
  KPI 曾是热窗截断下限、与审计库对不上的问题）。
- 审计 `append()` 在事件循环里同步打 Redis（pipeline 2 命令，中位 0.28ms），但
  `socket_timeout=2` ⇒ Redis 卡住时单请求最多阻塞 2s。**观察项**，见到抖动再上 threadpool。

## Architecture（一行一个文件 = 它的真角色）

| 文件 | 关键职责 |
|---|---|
| `src/gateway/main.py` | FastAPI 入口；所有路由 + 鉴权（强制 401，见上节）+ 审计 + L1/L2 调度 + 流式降级 + 推理透传；响应码捕获中间件（写 ContextVar → `gateway_requests_v2_total{status_code}`）。`/v1/*` 端点一律 `Depends(_bind_identity_ctx)`（async 上下文），**不要**用裸 `Depends(auth)`，否则准入身份丢失、全量 503 |
| `src/gateway/identity.py` | 客户端身份模型（纯本地，不发起网络调用）：`Identity` 只来自登记表；限流绑定键 `key-<12位id>-conf` / `fp-<sha256前16>-anon`，**字符集只含 `[A-Za-z0-9-]`**（冒号不可用）。`_bind_identity_ctx` 把身份写进 ContextVar |
| `src/gateway/policy.py` | YAML 策略引擎；**Pydantic 强校验**（`Policy` / `PolicyDoc`），错 action / 越界 priority / 缺 name 启动即抛 `ValidationError`；`decide()` 返回的 Policy 支持 `r["action"]` 字典式访问（routing.py 旧调用 0 改动） |
| `src/gateway/redaction.py` | 审计密钥脱敏：`log_entry()` 写 audit 前 in-place 替换 `text_preview` / `l2.explanation` / `reason` 等，覆盖 AKIA / PEM / ghp_/gho_/ghr_/ghs_/ghu_ / sk-ant- / sk-(proj-) / AIza / xox[abposr]- / JWT / Authorization Bearer。**PII 不脱敏**（身份证/手机/银行卡 走 route_local，audit 留原文方便追责） |
| `src/gateway/providers.py` | provider 注册表；`${VAR}` / `${VAR:default}` 展开；按 mtime 热重载；密钥只从 env 取。**写回 routing.yaml 必须走 `load_raw()` / `save_providers()`**（raw 未展开），否则 `${VAR}` 被展开吞掉 |
| `src/gateway/provider_keys.py` | `gateway_only` 凭据池（P1 只读 env）：per-provider 取 key，未登记身份 / 无凭据一律空串 fail-closed，**不抛错**（让 `validate_environment` 统一 503） |
| `src/gateway/llms/` | provider 线协议适配层（仿 LiteLLM llms/）：`base.py` BaseLLM 抽象；`openai_compat.py` / `anthropic_llm.py` 各实现一种 api_mode；`registry.py` 登记 api_mode→handler；`bridges.py` 承接入站协议桥（Anthropic Messages / OpenAI Responses ↔ 标准 chat）。**新增上游协议 = 加一个 BaseLLM 子类 + registry 登记一行，routing.py 零改动** |
| `src/gateway/routing.py` | `resolve()` 三级覆盖 + chat/embeddings/stream 转发 + Anthropic/Responses 协议转换；**单例 `httpx.AsyncClient`**（新代码别再 `httpx.AsyncClient()` 每请求）；`upstream_key()` per-provider 取凭据（**候选循环一律 `_request_credential`（客户端那半，每请求一次）+ `_cand_key`（网关那半，每候选一次）**——在循环里调 `upstream_key()` 会把上一候选的 key 打到下一家，且重复计入 `upstream_key_passthrough_total`）|
| `src/gateway/l2_retrain.py` | L2 影子一键优化管道：标注→数据集→GPU 微调→候选 8004 评测（battery+标注回归+历史分歧回归）→promote 写 `var/laya-l2.env`+重启 laya-l2；全局单 job，并发 409；**权威 backend 切换不走这里** |
| `src/gateway/env_file.py` | env 文件读改写（systemd EnvironmentFile 格式）：provider key 配置两层生效（os.environ 即时 + .env 行级持久化）；密钥明文永不回显 |
| `src/gateway/alias_router.py` | 别名组：`/v1/models` 暴露名 → (provider, model) 候选表（大小写不敏感，5s TTL 缓存，per-candidate 熔断）。`/v1/models` 单一真源 = `admin_api.build_public_models()`（别名组 + `internal_models` + 用户自注册，与 `/admin/api/models/public` 共用）；`internal_models` 只管暴露/展示/引用保护，**不参与路由**。改名走 `admin_store.update_alias_group(new_name=)`（组行+成员同事务搬移，撞名 422） |
| `src/gateway/inspection.py` | 文本层：Regex(身份证/手机/AKIA/私钥) + 熵 + Luhn(银行卡) + 关键词三档 + PII 加权评分（PII≥60 → route_local；risk≥20 灰区强制 L2，**无上界**）。pattern 跑 NFKC + 去分隔符 copy，**关键词跑原文**（英文词只写小写形态——子串匹配不 lower） |
| `src/gateway/file_inspector.py` | 文件层：PDF/Excel/Word/**图片（含 heic/avif）** + pymupdf 兜底 + 扫描件 OCR（paddleocr→pytesseract chi_sim+eng，5页/150DPI/8000字）；图片走 `_ocr_pixmap`（PDF/图片共享） + 20MB size cap；**解析在 `run_in_threadpool`**（不要直接 `await`）；扩展名不可信，以 magic 嗅探为准 |
| `src/gateway/small_model.py` | L2 `Qwen3-4B-Instruct-2507`；`CONFIDENTIAL_THRESHOLD=0.7`；LRU 500/TTL600；5 块并发；客户端必须复用；`latency_ms==0` 一律视为无真实调用（disabled / 缓存 / 空输入），不入直方图。**双后端** `AI_GATEWAY_L2_BACKEND=qwen\|laya`（laya = `laya-l2.service`:8003 `/classify`，noul 英文题规格）；影子双跑 `AI_GATEWAY_L2_SHADOW=true`（qwen 权威 + laya 并行打标，`shadow_*` 随审计 l2 JSON 自动落库，2s 短超时不拖主链） |
| `src/gateway/l2_overrides.py` | `/l2-config` 白名单键持久化：PUT 原子写 `l2_overrides.yaml`（`AI_GATEWAY_L2_OVERRIDES_PATH` 可覆盖），启动时 `apply_overrides()` 回灌 os.environ ⇒ 保存后重启仍生效。键必须是 `L2ConfigPutReq` 白名单（二次防线，禁任意 env 注入） |
| `src/gateway/responses_filectx.py` | codex `/v1/responses` 附件提取：`input_file` 块 filename 必取，`file_data` 仅 csv/txt/tsv 解析表头（pdf/xlsx 只看文件名）；prompt 里 `Files mentioned by the user` 段也提文件名 |
| `src/gateway/circuit_breaker.py` | 三态熔断 CLOSED→OPEN→HALF_OPEN；provider 级与 `prov::model` 级两套 breaker；admin 可手动 open/closed；生产阈值 2/30s/10s（`.env`，**不是**代码默认 5/60/30） |
| `src/gateway/stat_scope.py` | 统计口径唯一实现：`is_blocked`（block* 或 403）/ `is_local_route`（route_local）/ `is_abnormal`（拦截∪本地路由）/`resolve_action`；判口径只改这里 |
| `src/gateway/admin_store.py` | 明细库：`request_log`（key_id 稳定身份 + key_name 快照；`blocked_reason` 存 `l1:<规则名>`/`l2:…`；用量列 prompt/completion/cached/cache_creation）+ 聚合（`stats_group`/`stats_key_tier`/`stats_billing` 等）；MySQL 缺列启动时自动补（`_ensure_columns`），加列无需手写迁移 |
| `src/gateway/policy_admin.py` | policy.yaml 直接 CRUD（Pydantic 校验后原子落盘 + 缓存失效）；**更新必须传 `original_name`**，否则按新建判重名 422 |
| `src/gateway/rule_ai.py` | 规则 5.2「匹配项（AI 生成）」：管理员自然语言描述 → 调本地 L2（`small_model` 同一端点）生成 regex/contains 建议；**只生成不落库**（UI 预览后人工保存，失败手填兜底） |
| `src/gateway/model_policy.py` | 模型策略（external_candidates/internal_models/model_limits/load_balance_mode/review_model）：mtime 热重载，改配置不用重启；`model_rpm(provider, model)` 供限流卡口；单价单位 ¥/1M（×7.2 入库），另有 cached 档 |
| `src/gateway/stats_service.py` | 调节建议引擎 `build_suggestions`（规则 0 KEY 分级 / 4 优先级重排 / 5 沉默规则删（**新规则 14 天保底**：first-seen 落盘 `var/rule_first_seen.json` + ever-fired 区分，`AI_GATEWAY_SUGGEST_SILENT_GRACE_DAYS` 可调）/ 6 闲置白名单收走 30 天长窗；1 拉黑 / 2 IP 量 / 3 模型错误率 / 7 热点模型加限流用面板 hours 窗；阈值全 env 可调，**规则 1/2 支持按窗口 `24:20,168:100,720:500`**（纯数字=全窗口同值，`pick_window` 未登记窗口取最近））；一键应用 kind：black/white/unwhite/reorder/remove_rule/add_limit；`build_billing` 做 KEY×模型×单价金额。**计费已搁置**：内部估算口径，不追精度（精确数看上游账单），别再拆单价档 |
| `src/gateway/metrics.py` | `/admin/metrics` Prometheus；**双指标**：`requests_total`（legacy 5 轴，dashboard 不破） + `requests_v2_total`（P2 7 轴 `{type,action,rule,provider,model,local,status_code}`，status 由中间件写 ContextVar 供） + override_denied_total / fallback_total / tokens_total / l2_latency_ms / circuit_* / ocr_* |
| `src/gateway/metrics_ring.py` | 运行指标环：10s tick 差分 → 24h 内存环 + JSONL 落盘（`var/metrics_ring.jsonl`，kill -9 不丢）；P99 取近 5 分钟 `request_log`（`AI_GATEWAY_PCT_WINDOW_S`） |
| `src/gateway/audit_store.py` | 审计双写：Redis 热缓存 `key=gateway:audit` `lpush+ltrim(0,4999)`（影子统计只读这里）+ MySQL 持久化（后台线程批量写，fail-open，90 天清理；`l2` 列存主判+影子 JSON，缺列启动自检自动 ALTER）。`tail()` 按 `(LLEN, 最新一条)` 版本签名缓存解析结果（≈10MB/进程），**返回的是共享 dict 的浅拷贝——调用方只可幂等补字段**（现只有 key_name 兜底） |
| `src/gateway/admin_api.py` | 管理面：45+ `/admin/api/*`（Basic/`/admin/login` cookie，会话 TTL 见 `AI_GATEWAY_SESSION_TTL`）+ 运维三件套（`route-inspect` 干跑——文本外还支持 `channel=check/chat/responses` 文件干跑，codex∪workbuddy 并集、`providers/check` 健康、`circuit` 手动）+ provider 注册表 CRUD + provider key 配置（write-only，`env_file.py` 写 os.environ 即时生效 + 行级落盘）+ `stats/chart`（request_log 分桶，统计/总览折线图跟时间窗，桶内峰值 qps_max/lat_min-max）+ `l2-shadow-stats`（主影一致率/覆盖率，扫 Redis 热缓存）+ L2 影子看板（`l2-shadow-trend`/`l2-divergences`+标注/`export` JSONL，MySQL l2 列）+ `events` 降级事件流 + `rule-hits` 规则命中 + `policy/versions` 版本历史/回滚。**key 值永不回显**，ops-log 只记长度 |
| `src/gateway/rate_limit.py` | 令牌桶限流（in-memory）；`extract_client_key` 按 token > X-Forwarded-For > IP（XFF 仅 `AI_GATEWAY_TRUST_XFF=true` 时采用，默认 socket IP 优先）；`check_model` 模型级全局 RPM（配置在 `model_policy.model_limits`，卡口在 `routing.resolve` 出口，ContextVar 按请求去重） |
| `src/gateway/session_store.py` | 会话级机密标记：`X-Session-ID` 下 files/check 文件命中、chat/messages 内联图或提及数据文件名命中（route_local/block）后同会话强制 route_local（TTL 见 `AI_GATEWAY_SESSION_TTL`）；Redis + 内存兜底。另有**文件名污染**（客户端无感会话替代）：同 key 下曾判机密的文件名被正文提及即 `tainted_file_ref`（priority 12）走本地，按 key 隔离、后缀匹配 |
| `src/gateway/user_models.py` | `/v1/models/register` 自助注册；`AI_GATEWAY_EGRESS_DOMAINS` 白名单；Redis `gateway:user_models` |

路由：`/v1/chat/completions`、`/v1/embeddings`、`/v1/messages`、`/v1/messages/count_tokens`、
`/v1/responses`、`/v1/files/check`、`/v1/models`、`/v1/models/register`、`/v1/{path:path}`
（兜底转发，生产开 `AI_GATEWAY_STRICT_EGRESS=true`）、`/admin*`、`/health`、静态 `/`。

客户端形态（测试有覆盖）：workbuddy = OpenAI 协议（`image_url` 内联图走 OCR）；
codex = `/v1/responses` `input_file` 块（见 `responses_filectx.py`）。

## L2 训练与评测资产（`scripts/`，与 `src/` 同等重要）

- 服务：`laya_l2_server.py`（`POST /classify`）+ `laya-l2.service`（8003/GPU5，`LAYA_MODEL_PATH` 切 checkpoint，回退链只改这一行）。
- **权威 = qwen，laya 只是影子**（`AI_GATEWAY_L2_BACKEND=qwen` + `AI_GATEWAY_L2_SHADOW=true`，`var/laya-l2.env` 指当前影子权重）。**laya 判错不会让请求走本地路由**——真出境的是主判 qwen 和规则。别把"影子调优"读成"已切权威"（2026-09-29 就误报过一次）。`var/` 全在 `.gitignore` 里 ⇒ **现役权重指针不在 git**，换机器/重建容器时 `var/laya-l2.env` 会丢，必须先从 `~/models` 补回。
- 训练：`build_laya_dataset.py`（审计去重 + **人工标注 `l2_labels` 覆盖 rule 推导**（ground truth 最高优先级，人标 0 也盖掉规则 1）+ 模板合成）→ `~/models/laya-dataset/`；`laya_finetune_local.py <base> <data> <out>`（单卡 4090，**`CUDA_VISIBLE_DEVICES=5`**，与推理服务同卡，~10min @6.7k 条 4ep）→ `~/models/laya-finetuned-*`。**含真实审计内容的数据集/checkpoint 留 `~/models`，永不入库**；QUESTIONS 题规格在 server/train/eval 三处必须逐字一致。
- **挖数据/看数据**：`mine_audit_labels.py`（`--apply` 落标）把**规则推导覆盖不到的**（`rule` 不在 `POS_RULES|NEG_RULES`）审计行按"PII pattern→1、risk<20 且影子 p≤0.3→0、其余入 `var/label_queue.jsonl`"批量收进 `l2_labels`，队列由人工逐条裁决（标完删文件，下版 build 自动入集）。⚠️ **2026-09-30 实跑：492 行 → 去重仅 1 条可用文本、队列 0 条、477 条已人标跳过 ⇒ 池子见底，队列是兜底分支不是主路径**。主力仍是 `build_laya_dataset.py` 按审计 `rule` 字段弱监督批量构建。自动标 1 只认客观 pattern（"regex 不撒谎；keyword 只是词表不算证据"）；影子不可用（`p is None`）宁可不标。`shadow_pulse.py [hours] [--since]` 是轮统计唯一入口（裸 `--since` 读 `var/l2-rounds.jsonl`，与 `l2_shadow_stats`/`l2-divergences` 的 `since` 同口径）——**别再手拼 `?since=` URL**。
- **灌数分歧 ≠ 调参理由**：v30 上线首轮灌数出 1 条分歧（`分红明细：研发部 42%，市场部 23%，其他 35%` → L1 判密、影子 p=0.000 判放行），但三门全绿 + OOD 219/220 ⇒ **保留 v30 不回滚**（v28 三轮灌数全 0 分歧，但 OOD 216 / 标注回归 204/206，用结构性的抗注入能力换单点分歧是亏的）。灌数单轮只有 ~49 条可比，**样本量撑不起"为一条分歧训一版"**；真要动必须三门 + OOD + 灌数三样全跑，任一退步就留原版。L1 已判密的窄带里影子判错不构成上线阻塞（内容没出境，真出境的是主判 L2/规则）。
- **一键优化管道**（`src/gateway/l2_retrain.py` + 管理台 L2 影子页）：标注≥10 条 → `POST /admin/api/l2/retrain`（后台：数据集→GPU5 微调（`AI_GATEWAY_L2_TRAIN_GPU`，默认 5，与推理服务同卡）→候选 8004 临时服务跑 battery+标注回归+**历史分歧回归**（审计里 `label!=shadow` 的文本、sha1 去重、want 取自 `gray_flood.CANDIDATES` 前缀匹配，`text_preview` 截断的计 unmatched））→ `POST /admin/api/l2/retrain/promote`（写 `var/laya-l2.env` + 无密重启 laya-l2 + `/health` model 身份核对，见 `agents/vllm-hosts.md`；**终端跑一轮的登录/轮询/核对配方见 `agents/gotchas.md` 2026-09-29 条**）。**权威 backend 切换（qwen→laya）不在此管道，仍人工拍板**。qwen 提示词也在管道里：`POST /l2/retrain/prompt-test` 拿同 battery 给候选 prompt 打分（不落地，直调 `_call_qwen(system=)`，bypass 缓存/熔断/影子），通过再 `PUT /l2-prompts` 应用（即时生效 + 落盘）；训练报告 stamp 当时 `prompt_sha1`。
- **测试要审计落地**：进影子基线的 eval 流量必须经网关（`dev-key` + `vllm_local/qwen2.5:7b` 斜杠写法，主影双落审计）；直打 8003 `/classify` 只算模型 QA，不进 request_log/audit/影子统计窗（灌数范式 `scripts/gray_flood.py`：`inspect_text` 预筛 [20,60) → 网关 → 审计对账）。
- 评测：`build_chat_workdoc_testset.py` → `tests/fixtures/security_testset.jsonl`（160 条纯合成**可入库**，10 场景×8 问法×机密/放行）；`eval_security_testset.py`（L1 本地 + L2 走服务，**门逻辑必须与 `main._review_text` 同步改**）；`eval_laya_simulated.py <checkpoint>`（264 条）/`eval_laya_confidential.py`（8 探针 + 真实留出，**路径优先读 argv[1]、否则自动读 `var/laya-l2.env`**，别再硬编码版本号)/`eval_ood_holdout.py`（220 条分布外：base 160 + 抗诱导/长填充等 6 组×10，laya 走 `OOD_L2_URL`（默认现役 8003，量候选版指 8004）+ qwen 权威对照）。
- **体检候选一条命令**：`scripts/l2_check.py [版本号]`（不带参数 = 盘上最新版）打印三门 → 显式 PID 起 8004 加载候选 → 候选与现役各跑一遍 OOD → 并排对比（**漏报/误报分列，比较先看漏报**）。不训练不晋升。收尾只 `kill $CAND`，**别用 pgrep**。
- **判定流量按轮读，不按小时窗**：灰区灌数是一轮 ~50 条集中在 40 秒内、其余时间零流量，按 1h/6h 窗看会误读成"没数据/一致率变差"（7d 窗 86.9% 里混着早期调试流量）。轮次历史在 `var/l2-rounds.jsonl` + `GET /admin/api/l2-rounds`（管理台"灌数轮次"表），每行带当时现役影子版本。
- **hard 数据铁律：抗诱导/前缀类样本必须"内容×前缀"配对**（同句内容只换前缀、标签只跟内容走，含无前缀基线）。只喂单侧（v28 十条"勿外传+密内容"）模型会把**字面前缀**当特征：v29 反向加 3 条"勿外传+非密内容"就翻成"勿外传⇒放行"，`inj_prefix` 漏报 4 条（安全方向变差）；v30 铺 18 条配对矩阵后 `inj_prefix` 10/10、OOD 219/220。**漏报方向优先**：误报可接受、漏报不可接受（比较版本先比漏报条数）。
