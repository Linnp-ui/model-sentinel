# Gotchas（坑位记录）

> 上级：`AGENTS.md`。按时间倒序读也行，按主题扫也行。

- **2026-09-21 — request_log 曾把"过链路的 200 请求"记成 block（已回填订正）**：
  2026-09-15 前中间件靠 `gw_block_reason` 是否非空推断 request_log.action，凡过
  L1/L2 链路的请求（含 `l2:无敏感信息` 放行、降级本地 200）都记 `action=block`
  （09-14 一天 1135 条 block+200），统计"拦截量"（`stat_scope.is_blocked` =
  block* 或 403）被灌到 7d=1158。09-15 起 `_mark_gw_action`（main.py）+
  `resolve_action`（stat_scope.py）改同源，**09-16 起数据干净**。2026-09-21 回填：
  1156 条 block+200 按 reason 订正——L2 非敏感（`l2:无敏感信息`/`l2:内容无敏感信息`）
  772→allow；本地承接（`financial_local_only`/`pii_weighted_route_local`/
  `l2:含图号和工艺参数`/`block_secrets`/e2e 探针）384→route_local；真硬拦
  （403/401/503）保留 block。备份表 `request_log_bak_20260921`（确认无误后可 drop）。
  审计侧从未污染（action 恒取决策值，降级记 route_local+downgraded_from），但
  request_log 与 audit_logs **覆盖范围本就不同**（POST 推理 vs 决策入口），总量
  不必强对齐；两页 block 数对不上时先确认窗口是否含 09-16 前历史数据
- **2026-09-20 — request_log 身份按 key_id（kid），name 只是快照**：`request_log`
  有 `key_id`（=api_key_map.id，稳定身份）+ `key_name`（写入时快照）。统计/过滤一律
  在 `admin_store._log_rows` 里按 `key_id` join `api_key_map` 解析成**当前名**再分组
  （`_resolve_key_name`），所以**改名只改 api_key_map.name，历史行不重写**，占比图自动
  按新名合并；已删除的 kid 回退显示存储的旧名快照。别再把 `key_name` 当身份键分组、
  也别在改名时批量 UPDATE 历史行（旧 Option A 已删）。未登记请求 key_id=NULL
- **2026-09-20 — 时间是"存 UTC、显北京时间"**：服务器时区 UTC，审计
  `time`/`audit_logs.ts` 与 `request_log.ts` 落库都是 UTC（naive）。**所有展示层**
  +8h 对齐北京时间：审计 `/admin/audit/entries` 的 `entries[].time`、`/admin/csv`
  （MySQL+Redis 两分支，经 `main._to_beijing`）、analytics 趋势时段桶 `time_slots`
  标签、request_log `/admin/api/logs` 的 `ts` 与 `/stats/daily` 日期桶（经
  `admin_store._ts_beijing`）。`since/until` 窗口过滤与分桶 idx **仍按 UTC**（与存储
  口径一致，别动）。**别把 +8h 挪到写路径**（`log_entry` 打 time / `log_request_async`
  打 ts 处），否则存量与新数据口径不一致、过滤全偏 8h。两个 helper 同口径但各自独立
  （admin_store 不 import main，避免循环）
- **2026-09-20 — 桥接 responses 曾恒发静态 `resp_gateway` id**：chat→responses
  流式桥接（`routing._responses_from_chat_stream`）构造 `ResponsesSSEAssembler` 时
  不传 `resp_id`，回落默认 `"resp_gateway"`——**同会话每个轮次共用一个 response id**。
  codex-rs 按 id 区分会话轮次，静态 id 让整段对话无法收尾/复制回复/分支到新聊天
  （表现"像被中断"）。只在走本地桥接时出现（机密会话粘性 route_local 后整段都走桥接）；
  直通透传上游（deepseek 原生 /v1/responses）用上游唯一 id 不受影响，故"只有用网关
  且内容机密时"才复现。已改每次请求 `resp_+uuid4`（2026-09-20 修）
- **2026-09-20 — 会话粘性不触发先查两处**：①客户端是否真传 session 头（网关只认
  `x-session-id` / `x-gateway-session-id` / `x-conversation-id`；且审计 API 曾不返回
  `session_id` 字段，查不到不等于没传，先看接口字段）；②chat/messages 内联图/粘贴
  表格命中 2026-09-20 前不写 session 标记（只有 files/check + responses 写），当次
  转本地、下一句照样外发。现 chat/messages 命中即标（`_mark_session_hit`）：内联图
  直接标；纯文本需正文提及数据文件名（`_mentioned_data_filename`，csv/tsv/txt/xlsx/
  pdf/docx 后缀）才标——workbuddy 发 CSV 走「文件路径：…+ 全文粘贴」无字节附件，
  只有文件名可依赖。审计 preview 只存 200 字，判断有没有图/附件要看 request_log
  的 prompt_tokens（几万 token = 粘贴整表）或直查 session 存量 `is_confidential`
- **2026-09-20 — codex 不发会话头也不发 previous_response_id**：887/887 responses
  请求 session 头全空，且 `previous_response_id` 恒 None（codex 每轮**全量重发**
  input，含 function_call_output 文件内容）。所以 ① 机密内容每轮都会被 L1/L2 重新
  命中（这才是 codex 的主保障，粘性是冗余+归组）；② 会话标识用
  `main._codex_session_id` 取对话**首条 user 消息** sha256 前 16 位（`codex-<hash>`），
  不能用 previous_response_id（每轮指向上一轮响应，非稳定会话号）。审计 responses
  正常路径（流式/非流式最终 log_entry）曾漏写 `session_id`，已补
- **2026-09-20 — L1 曾无手机号 pattern**：`PATTERNS` 只有身份证/密钥类，
  裸手机号 `risk=0` 原文出境到外网（生产实测）。已补 `1[3-9]\d{9}`（权重 60，
  单命中转本地）+ 关键词 `手机号 20`。教训：AGENTS 写"Regex(身份证/手机…)"
  不等于真有，改 L1 先跑 `inspect_text` battery（见本轮 tests 第 1 节），别信文档
- **2026-09-20 — 测试身份证须校验位合法**：`idcard` pattern 有 `idcard_valid`
  校验，`…011234` 这类随手编的号会被静默跳过（只剩关键词分），用例须用
  `…011237` 等真合法号。Embeddings 同理
- **2026-09-20 — P99 口径已换**：`latency_percentiles` 改算 `gateway_internal_ms`
  （旧值含上游生成，P99 曾达 12s；现 p50≈3ms / p99≈230ms）。看历史 dashboard
  曲线断崖别慌；`avg_ms`（统计概览）仍是总耗时，未换
- **2026-09-20 — shell 里查 `request_log` 先 export**：`.env` 不会自动加载，
  `ADMIN_DB_URL`/`DATABASE_URL` 为空时 `AdminStore` 静默用内存后端（重启即丢，
  查出来全 0 还以为没流量）。`os.environ.setdefault` 读 `.env` 前两行即可，
  别打印值

- **policy.yaml 与 policy.py 必须同步部署**：`when` 条件的新运算符（如 `==`）在
  `policy.py` 的 `_eval_cond` 里实现；只传 yaml 不传 py 会**静默失效**（条件永远不
  匹配，不报错）。P2 起 Pydantic 强校验覆盖了字段名 / action / priority / name 的
  拼写错误，但 `when` 内部运算符仍是自由字符串解析，不会报错
- **policy.yaml 修改后必须重启**（P2 改 fail-fast）：`load_policy()` 启动 / 每次请求
  用 Pydantic 强校验；写错 action 名（不在 `allow`/`route_local`/`block`）/ priority
  越界（0..1000）/ 缺 `name` 都会立刻抛 `ValidationError`，不再静默失效
- `routing.yaml` 的 `default_external` / `default_local` / provider `local: true/false` /
  `policy.target` 四者不一致是常见 bug 来源；改完跑 `tests/test_gateway.py` 验证覆盖
- 大文件限 50 页 PDF / 100 行 × 20 列 Excel；文件解析在 `run_in_threadpool` 不要直接 `await`
- `policy.yaml` 的 `text matches_regex` 需用单引号包 YAML 字符串（避免 `\` 被吃）；
  复杂正则可考虑写到 `inspection.py` 而不是 `policy.yaml`
- `@app.on_event("shutdown")` 在 FastAPI 0.115+ 已 deprecate（测试 warnings 里有，可忽略）
- 流式降级触发：`prompt_too_long` / `5xx` / `breaker_open` → `vllm_local` 兜底；SSE 里
  会多一段 `chatcmpl-fallback` 事件告知
- `auth()` 强制鉴权（无 key 401；旧"已关闭/BYOK"说法已过期）：`Bearer` / `x-api-key`
  两头都认；黑名单 deny-overrides（只看 Bearer 会被 `x-api-key` 绕过，2026-09 修）。
  生产 `gateway_only` 下外发额度仅限具名登记身份，dev key 外发 503 fail-closed
- `requirements.txt` 固定 `fastapi 0.115.* / uvicorn 0.30.* / pydantic 2.9.*`；
  `pytesseract` 已在内（配系统 tesseract + chi_sim 即真 OCR）；
  `pytest` / `reportlab` / `paddleocr` 不在，测试或重 OCR 需要单独装
- **P0 红队 (2026-09-07) — PII regex 抗 Unicode 绕过**：`inspection.inspect_text`
  入口对数字类 PII (idcard/phone/bankcard) 先做 `NFKC normalize` + 去
  不可见字符 (`U+200B-F` / `U+202A-E` / `U+FEFF` / `U+2060-4` 等) + 去
  常见分隔符 (`- _ . / | ,` + 全角空格)。所以 `110101-1990-0101-1234` /
  `11010\u200b1199001011234` / 全角数字全部命中 PII。**关键词扫描仍跑
  原文本**（中文关键词不能 NFKC 掉）；其他 PII (email / AKIA / ghp_) 跑 NFKC copy
  不去分隔符。**文件类回归在 `tests/test_expanded.py` 第 8 节**
  （codex `input_file` / docx / 图片真 OCR / workbuddy `image_url`），`policy.yaml` 多了
  `ocr_empty_image_route_local (priority 12)` 规则
- **P0 红队 (2026-09-07) — 文件扩展名不可信**：`file_inspector._sniff_magic`
  用文件头 magic bytes 嗅探真实类型 (PNG/JPEG/PDF/XLSX/...)；用户声明
  `.txt` 但内容是 PNG/PDF/XLSX 二进制 → **强制走对应分支**（之前是
  走"通用文本"拿一堆 null 字节当文本，**PII 静默放行**）。在
  `inspect_file()` 入口 + image 路由处都加了 mismatch 检查 + findings flag
  `ext_mismatch / magic_ext / claimed_ext`
- **P0 红队 (2026-09-07) — OCR 失败时 fail-CLOSED**：`inspect_image()`
  OCR 返回 `text=""` 时设 `findings.ocr_empty_and_image=True`；policy
  规则 `ocr_empty_image_route_local (priority 12)` 命中即 `route_local`。
  三种 fail-OPEN 场景都封死：OCR 引擎没装 / 图损坏 / HEIC/AVIF 等不支持
  格式。**测试 `test_inspect_image_corrupted_fail_open` 已覆盖**
- **P1 红队 (2026-09-08) — 会话 ID 是客户端自填的**：上传机密文件命中
  route_local/block 时给 **session** 打标（`gateway:session:<id>`，TTL 见 .env），
  同 session 后续请求走 `session_route_local (priority 5)`。**换 session ID / 不带
  session 头可绕过** —— 曾加 client 身份绑定标记（`gateway:client:<fp>`，无 session
  头也照打）堵死，但 **2026-09-18 上线前整体移除**：标记强制 7 天 route_local，
  而 route_local 计入黑名单建议引擎的"异常处置"，形成"上传一次 → 全本地 →
  攒够 20 次 → 被建议拉黑"的误报回路。若日后恢复，须先让建议引擎排除
  `rule=session_route_local` 的计数
- **P1 红队 (2026-09-08) — XFF 不可信**：`main._resolve_client_ip()` socket IP
  优先；XFF 仅 `AI_GATEWAY_TRUST_XFF=true`（反向代理部署）时采用。`_client_ip()`
 （审计）与 4 处 rate-limit 统一走它。之前审计日志的 client_ip 可被任意伪造，
  现已堵死。注意之前报的"XFF 轮换绕限流"是误报（TestClient 伪影）：生产一直是
  socket IP 优先，XFF 只在 `request.client is None` 时兜底
- **P1 红队 (2026-09-08) — /admin* 局域网全可读**：`admin_auth` 默认恒放行
  `10./192.168./169.254.`（办公网 dashboard 便利），生产 `.env` 未设
  `AI_GATEWAY_ADMIN_IP_ALLOWLIST`，故整个办公网可读 `/admin/csv`（含 PII 预览）。
  新增 `AI_GATEWAY_ADMIN_STRICT_LAN=true` 收紧开关（默认 false 保持行为）；
  **网关纯内网，LAN 全员即用户群，STRICT_LAN 保持 false 不翻**（翻了办公网
  dashboard 全 403）。以后要卡某台机器再把 allowlist 写死 + 翻开关
- **部署漂移事故 (2026-09-08)**：本地 `main.py` 已超前生产多个版本（P2 的
  metrics v2 / redaction），只推 `main.py + session_store.py` 导致生产 crash-loop
 （`ImportError: get_current_status`，restart counter 10 次后 failed）。教训：
  **推 main.py 前必须先全量对 md5**，把 main.py import 的新符号
  所属文件（`metrics.py` / `redaction.py`）一起推；推后**先远端 import 再 restart**，
  不要依赖 systemd 重试。生产当前仍落后本地：`llms/*` / `policy.py`(Pydantic) /
  `providers.py` / `routing.py` 未同步（P2 功能在生产未生效），下次大版本一起推
- **OCR 引擎缺失时 fail-CLOSED 转本地**：`inspect_image()` / 扫描件 PDF 的 OCR
  返回 `text=""` 时设 `findings.ocr_empty_and_image=True`，规则 `ocr_empty_image_route_local
  (priority 12)` 命中即 `route_local`（三种 fail-OPEN 场景都封死：引擎没装 / 图损坏 /
  超 20MB）。**缺引擎不再静默放行，但本地模型也看不懂图** —— 生产网关的 `.venv`
  仍要装 `pytesseract + 系统 tesseract-ocr + chi_sim`（已进 requirements）或
  `paddlepaddle + paddleocr`，否则扫描件/截图类请求全走本地降级。部署后用一张已知含
  文字的图做 `curl -F file=@test.png /v1/files/check`，看 `findings.char_count > 0` 验通
- **图片支持含 heic/avif**：`file_inspector._open_heif_image` 已接（曾缺失，
  iPhone 截图直放行过）。无 OCR 引擎 / 图损坏 / 超 20MB 一律 fail-closed
  转本地（`ocr_empty_image_route_local` / `unparsed_binary_route_local`），不 500 不挂起。
  生产 OCR 能力仍需单独保障（见下条），否则扫描件只能走"转本地"兜底
- **图片 size cap = 20MB**：`_IMAGE_OCR_MAX_BYTES`；超大图直接 fail-open 返空 +
  `ocr_errors_total++`，不解码、不进引擎（防 OCR 炸弹）。调整需同步
  `test_inspect_image_oversize_no_ocr`
- **多页 TIFF 只 OCR 首帧**：`_ocr_image_bytes` 取 `doc[0]`；多页 TIFF（扫描仪常见
  输出）只在 `findings.char_count` 体现第一页。要支持多页需循环 `len(doc)` 累加
  text 并按页码加 `[OCR_PAGE i]` 标记（参考 PDF 路径的 5 页限制）
- `.env` 与 `AGENTS.md` **均已入库**（早期在 `.gitignore`，说法已过时）；生产用
  SSH Key / Vault 注入密钥，`routing.yaml` 永远只写 `api_key_env`
- **DeepSeek 模型名**：`deepseek-chat` / `deepseek-reasoner` 在 2026-07-24 已退役，
  V4 唯一 ID 是 `deepseek-v4-flash` / `deepseek-v4-pro`；`routing.yaml` 已切到
  `api.deepseek.com/v1` + `DEEPSEEK_API_KEY`，OpenRouter slug `openrouter/deepseek-v4-flash`
- **P2 Prometheus 查询**：status 由 `main.py` 的 `_capture_response_status` 中间件写
  ContextVar 提供；测试 / 非 HTTP 路径走 `_infer_status()` 从 `action` / `rule` 推断。
  `status_code="0"` 表示未知（极少数 fallback 路径）
- **audit 里的 secret**：`log_entry()` 落 audit 之前过 `redaction.py`，AKIA / PEM /
  GitHub PAT / sk-ant- / sk-proj- / AIza / xoxb- / JWT / Authorization Bearer
  mask 成 `[REDACTED:<kind>]`。**PII 不脱敏**（命中走 `route_local`，audit 留原文
  方便追责；BYOK 模式责任在客户端）
