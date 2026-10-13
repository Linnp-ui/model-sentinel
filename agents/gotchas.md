# Gotchas（坑位记录）

> 上级：`AGENTS.md`。2026-09-29 精简：删 v1–v9 训练迭代流水（已被 v30 定版取代，
> 完整版在 git 历史里），压事故叙事留结论，规则一条未删。
> 按时间倒序读也行，按主题扫也行。

- **2026-10-12 — 宿主 TZ 切 Asia/Shanghai，时基全量锚定 UTC**：起因是"系统展示时间
  全部北京时间"。改法 = 写入侧 16 处裸 `strftime`/`now()`/`localtime` 全部显式化
  （审计 `main.log_entry`、request_log 兜底、admin 元数据 8 表、`stats_service._window`/
  purge、`/logs/cleanup`、`metrics_ring` P99 cutoff、`l2_retrain` 报告 ts），展示侧统一
  `to_beijing +8h`（keys/keyrules/policy·prompt 版本/规则命中 last_seen + 前端图表 x 轴
  `bjAxis`、降级事件流表格），`_parse_window_ts` 重构：存储值按 UTC 原样、用户朴素日期
  按北京（`naive_as_utc=False`）、ISO 带偏移精确换算；删死码 `to_local_naive`。**回归
  测试**：`tests/test_tz_explicit_utc.py` 在 `TZ=Asia/Shanghai` 下断言锚定（全量
  155 passed）。**crontab 已转北京墙钟保绝对时刻**（vllm 日报 `16:00 UTC` → `0 0`）。
  坑：切 TZ 后**已跑进程要重启才吃新 TZ**；
  MySQL 在 docker 自带 `TZ=Asia/Shanghai`，`NOW()` 口径不变、与宿主切换无关。

- **2026-10-08 — 审计数据谱系**（「一共多少条 / 为什么数字对不上」类问题查这条，别重新考古）：
  `audit_logs`（90 天留存）是唯一持久审计库；Redis 热窗 ltrim 封顶 5000（进新出旧，窗口滑动，
  被裁的行只存在于 audit_logs）。**09-01~09-11 静默丢失时代**：writer 启动无重试（MySQL 离线
  则整个进程生命周期静默缺失）+ 熔断期丢行且无计数无日志，598 条只进 Redis；09-30 修复（启动 60s 重试 + 五类丢行计数 + 多行 VALUES），2026-10-08 用
  `scripts/backfill_audit_redis.py`（幂等）回填完毕，此后热窗与审计库逐日 diff=0。
  最老可恢复记录 = **2026-09-01 07:18:41**；08-18（服务首启）~09-01 是**证据盲区**
  （request_log 09-10 才开始、journal 不记网关请求），5000 是历史总量**下界**。
  `req_id` **非唯一**，对账/回填必须先 SELECT 已有再插缺的，INSERT IGNORE 不行。
- **灰区上界曾用 `risk_score < 60`，只对纯 PII 成立（2026-09-30 已修）**：
  `risk_score = weighted_pii_score + keyword_score`，而唯一按分值判的 policy 规则
  （priority 5）读的是 **weighted_pii_score，不含关键词分** ⇒ 「关键词堆叠到 >=60」
  既超灰区上界（L2 不触发）又匹配不上 priority 5（L1 放行），**两条路都漏，内容直出外网**。
  实测「工资 薪资 奖金」kw=60 → `allow`/deepseek；而单词「机密」kw=30 → `route_local`
  ——**堆得越多越危险**（设计意图是"30 档两命中=60 转本地"，实现读错了字段）。
  **修**：`main._l2_gray_trigger` 去掉上界，只保留下限（`main.py:807`）。
  **上界对 PII 本就是死代码**——PII>=60 时 priority 5 已判 route_local，
  门的第一条件 `action == "allow"` 就是 False。回归见
  `test_l2_gray_trigger_no_upper_bound_on_risk`（已验证：回退修复该测试转红）。
  代价（2026-09-30 实测）：160 条测试集 L2 调用 79→103；**生产近 7 天审计 1095 条里
  risk>=60 占 23 条（2.1%）** ⇒ L2 量 +2.1%，可接受。
  ⚠️ **剩下的敞口不在这条**：测试集 7 条漏网全是 `risk=0 l2p=noL2`
  ——L1 零特征命中时 L2 根本不被调用（"38 后面加个 k"这类纯语义转写）。
  要堵它得**对全流量跑 L2**，是成本/口径决策，**没做**。

- **L2 判定缓存不含 prompt 版本（改 prompt 必须重启）**：`_CACHE`（`small_model.py:86`）
  键是 md5(`text[:800]`|filename|headers|sheet_names)，**不含 prompt_sha1**——
  键要跨进程稳定，塞易变 hash 会让重启前后命中率归零。
  ⇒ `PUT /l2-prompts` 应用新 prompt 后，**同一文本在 TTL 600s 内仍返回旧结论**。
  症状：`prompt-test` 打分通过、干跑同一 battery 却仍报 NORMAL（2026-09-28 v3 落地时踩的）。
  解法：重启 gateway（`apply_overrides()` 在 startup 回灌）或手动清 `_CACHE`；
  **无自动失效机制，别指望热加载能覆盖它**。
  同族：`l2_retrain` 里的 prompt 打分是**直调 `_call_qwen(system=)` bypass 缓存**的，
  就是为了让候选 prompt 的分数不受旧条目污染——写新测试/改 prompt 时照这个思路。

- **2026-09-28 — 测试洗掉线上 v1 prompt（`l2_overrides.yaml` 是生产共用真文件）**：
  conftest 的隔离只盖 DB/HTTP，**碰仓库真文件的后端（`_l2ov`）测试必须打 fake**
  （`test_l2_prompt_versions_roundtrip` 已改 `monkeypatch.setattr(_aa, "_l2ov", FakeOv())`）。
  修后从本地备份重 PUT 恢复，文件入库锁定 v1。

- **2026-09-28 — /admin/csv 恒 500（瘦身重构丢初始化行）**：`486bf71` 重构
  `export_csv` 时丢了 `cols`/`_csv_safe`/`buf = io.StringIO()`/`w = csv.DictWriter(...)`
  四行 → `NameError: buf`，MySQL/Redis 两路全 500（按钮不报错，新窗口直接 500 文本，
  无人发现）。教训：瘦身/搬移函数体后对该端点做一次真实调用（两路参数都打）；护栏
  测试 `test_admin_csv_export_shape`（同 `test_admin_metrics_timeseries_shape` 的
  "端点无覆盖"模式）。

- **L2 影子训练史 v1–v9（全部退役，现役 v30，见下方"定版"条）**：留的是**仍然生效的
  方法论与陷阱**，版本分数一律不记。
  - **影子期只看两个数**：① qwen=NORMAL 且 `shadow_conf≥0.7` 的**误报率**（技术类内容
    重灾区）② qwen=CONFIDENTIAL 且 `shadow<0.5` 的**漏报率**。误报率不可接受就微调或
    放弃切换；达标才谈 `AI_GATEWAY_L2_BACKEND=laya`。
  - **cells 口径**：`cc`=主密影密 / `cn`=主密影放（影子漏报）/ `nc`=主放影密（影子误报）
    / `nn`=主放影放；覆盖率=`shadowed/l2_total`；一致率=`(cc+nn)/compared`
    （`main_degraded` 不计入）。
  - **题规格只有 noul + 英文题最优**：中文 choice 题整体偏弱，普通中文技术文档/讨论会
    误高（与真阳性区间重叠，单靠阈值分不开）。
  - **checkpoint 只认英文**：非拉丁脚本准确率 0@置信度 0.95，必须用 `multilingual`
    子目录；出厂 temperature 未拟合（全 1.0），概率可信前在自己数据上 refit。
  - **服务渲染必须与训练/评测布局一致**：`_render_state` 曾恒渲染空字段
    （`文件名: \nSheet: \n表头: \n`），同 checkpoint 同文本 CAP 从评测格式 0.013 抬到
    服务格式 0.96——灰区误报放大器。已改空字段不渲染。
  - **探针别带人工 ID 前缀**：laya 是 argmax 确定性推理，`灰区B{n}:` 这类前缀会扰动
    输出 = 测试假象，复测用裸话术。
  - **模型被修饰词劫持，不是看 payload**（`~/models/public-datasets/torchsight` swap 实验，Apache-2.0）：同 SSN+薪资 payload，
    评语 "Below Expectations"→"Needs Improvement" 一词之差让结论 0.00→0.84，换名无
    影响。训练须加"同 payload × 不同修饰"对称样本。
  - **小决策模型打地鼠**：v1–v9 每轮修好目标切片必动别处（v6 修好 swap，pii 95.5→82.6、
    medical 83.5→47.5）。所以别把单组分数当结论。
  - **分歧明细数据源**：`audit_logs.l2` 列 2026-09-28 才加，此前影子数据只在 Redis
    `gateway:audit`（5000 条）——老窗口的 nc/cn 原文要直接扫 Redis。
  - **在线复测用 `scripts/eval_laya_live.py`**（打 8003 直测，渲染与离线 `eval_laya_confidential.render` 逐字一致）；直打不进审计，
  进影子基线的 eval 流量必须经网关（见 AGENTS.md）。
- 回退链只改 `var/laya-l2.env` 的 `LAYA_MODEL_PATH` 一行。

- **2026-09-24 — revert 必须整块（半截 revert 出过 500）**：`metrics_timeseries` 切方案
  时只恢复函数签名、留下切片/抽稀引用未定义的 `hours`/`RING_POINTS`，线上
  `/admin/metrics/timeseries` 全 500（总览+运行指标双双刷不出）。教训：revert 按 diff
  块整个消，签名和函数体分两次改必留半截；改完先本地复调端点再重启（该端点无测试覆盖，
  当时靠用户控制台报错才发现）。

- **2026-09-24 — `CircuitBreaker.state()` 读即迁移**：`state()`/`allow()` 在 OPEN 且冷却
  到时当场迁 half_open 再返回 ⇒ RECOVERY=0 时永远报不出 open（刚重开也被读回
  half_open）。写单测/看快照注意。别名 503 信封曾误标（预钉的 default_external 沿用），
  已修：`RoutingError.provider` + `_err_provider(exc, prov)` 统一；审计侧 `provider`
  仍是预解析值（真别名在 `requested_model`）。

- **2026-09-24 — 管理台禁用 `sticky`，统一真 fixed + 实测让位**：sticky 在 body 滚动 +
  flex 下每帧重算位置会抖（侧边栏/统计工具条/顶栏三连中招）。现统一：Sider 真
  `position:fixed`（`marginLeft:200`）、顶栏包 `.app-topbar` 真 fixed（`left:200px`），
  高度用 ResizeObserver 实测后内容区 `paddingTop` 让位。页面工具栏走标题右插槽
  `#page-toolbar-slot` + 各页 `createPortal`，随页卸载自动清空。`theme.css` 只剩
  `.app-topbar` 一条定位规则，别再加逐行 sticky 偏移。

- **2026-09-21 — 一键 apply 改了生产的 `policy.yaml` / `model_policy.yaml` 必须顺手提交**：
  建议引擎 reorder/add_limit 落盘的就是入库配置文件，不提交会让仓库版与生产版漂移。
  apply 成功后 `git diff policy.yaml model_policy.yaml` 看一眼，确认预期就提交。

- **2026-09-21 — `upsert_policy` 更新必须传 `original_name`**：不传按新建走唯一性检查，
  直接 422 `rule name already exists`。更新接口（`PUT /policies/{name}`）已传；新增
  apply 分支调 `upsert_policy` 改 priority 时照抄该写法。

- **2026-09-21 — 测试共用 memory store，残留行会抢 Top 位**：同 session 内各用例 seed 的
  行全在一张表里。按 Top1/总量断言的用例（规则 7 加限流）必须 seed 唯一 label 且播足量
  （100 行压住残留），断言按建议 id/type 过滤、按"已配的不再出现"收敛，不按"建议消失"
  （下一个热点顶上来是正常行为）。

- **2026-09-21 — SuggestionCard 原样转发 `s.apply`**：前端 `JSON.stringify(s.apply)` 直发，
  后端 `SuggestionApplyReq` 字段名必须与引擎产出的 apply key 一致（`limit_model/limit_rpm`，
  不是 `model/rpm`）。加新 kind 先对齐两端命名。

- **2026-09-21 — request_log 曾把"过链路的 200 请求"记成 block（已回填订正）**：2026-09-15
  前中间件靠 `gw_block_reason` 非空推断 action，凡过 L1/L2 链路的请求（含
  `l2:无敏感信息` 放行、降级本地 200）都记 `action=block`，统计拦截量被灌到 7d=1158。
  09-15 起 `_mark_gw_action`（main.py）+ `resolve_action`（stat_scope.py）改同源，
  **09-16 起数据干净**；09-21 已按 reason 回填 1156 条（备份表
  `request_log_bak_20260921`，确认无误后可 drop）。审计侧从未污染，但 request_log 与
  audit_logs **覆盖范围本就不同**（POST 推理 vs 决策入口），总量不必强对齐；两页 block
  数对不上时先确认窗口是否含 09-16 前历史数据。

- **2026-09-20 — request_log 身份按 key_id（kid），name 只是快照**：统计/过滤一律在
  `admin_store._log_rows` 里按 `key_id` join `api_key_map` 解析成**当前名**再分组
  （`_resolve_key_name`）⇒ 改名只改 `api_key_map.name`，历史行不重写、占比图自动按新名
  合并；已删除的 kid 回退显示旧名快照。别把 `key_name` 当身份键分组，也别在改名时批量
  UPDATE 历史行。未登记请求 `key_id=NULL`。

- **2026-09-20 — 时间是"存 UTC、显北京时间"**：审计 `time` / `audit_logs.ts` /
  `request_log.ts` 落库都是 UTC（naive）。**所有展示层** +8h：审计 `/admin/audit/entries`
  的 `entries[].time`、`/admin/csv`（两分支，经 `main._to_beijing`）、analytics 趋势时段桶 `time_slots`
  标签、`/admin/api/logs` 的 `ts` 与 `/stats/billing` 按天桶（经 `admin_store._ts_beijing`）。
  **`since/until` 窗口过滤与分桶 idx 仍按 UTC**（别动）。**别把 +8h 挪到写路径**，否则存量
  与新数据  口径不一致、过滤全偏 8h。两个 helper 同口径但各自独立（admin_store 不 import
  main，避免循环）。**别动写路径**（`log_entry` 打 time / `log_request_async` 打 ts）。
  MySQL `NOW()` 是北京时间，ad-hoc SQL 用 `ts > NOW() - INTERVAL n` 恒空 ⇒ cutoff 用
  Python `utcnow()`，或显式写 `NOW() - INTERVAL 8 HOUR`。
   **2026-10-12 更新**：宿主已切 `Asia/Shanghai`，"UTC 落库"从今起靠**显式 gmtime/utcnow**
   维持、不再靠"宿主=UTC"巧合；用户朴素日期输入改按北京解释（`_parse_window_ts` 加
   `naive_as_utc` 参数），细节见 2026-10-12 条。

- **2026-09-20 — 桥接 responses 曾恒发静态 `resp_gateway` id**：`routing._responses_from_chat_stream`
  构造 `ResponsesSSEAssembler` 时不传 `resp_id` ⇒ 回落 `"resp_gateway"`，同会话每轮共用
  一个 response id。codex-rs 按 id 区分轮次，静态 id 让整段对话无法收尾/复制/分支（表现
  "像被中断"）。只在走本地桥接时出现（机密会话粘性 route_local 后整段走桥接）；直通透传
  用上游唯一 id 不受影响。已改每次请求 `resp_+uuid4`。

- **2026-09-20 — 会话粘性不触发先查两处**：① 客户端是否真传 session 头（网关只认
  `x-session-id` / `x-gateway-session-id` / `x-conversation-id`；审计 API 曾不返回
  `session_id` 字段，查不到不等于没传）② chat/messages 内联图/粘贴表格命中 2026-09-20 前
  不写 session 标记。现 chat/messages 命中即标（`_mark_session_hit`）：内联图直接标；纯
  文本需正文提及数据文件名（`_mentioned_data_filename`）才标——workbuddy 发 CSV 走
  「文件路径 + 全文粘贴」无字节附件，只有文件名可依赖。审计 preview 只存 200 字，判断有
  没有图/附件要看 `request_log.prompt_tokens`（几万 token = 粘贴整表）或直查 session 存量。

- **2026-09-20 — codex 不发会话头也不发 previous_response_id**：887/887 responses 请求
  session 头全空、`previous_response_id` 恒 None（每轮全量重发 input）。所以 ① 机密内容
  每轮都被 L1/L2 重新命中（这才是 codex 的主保障，粘性是冗余+归组）② 会话标识用
  `main._codex_session_id` 取对话**首条 user 消息** sha256 前 16 位（`codex-<hash>`）。
  审计 responses 正常路径曾漏写 `session_id`，已补。

- **2026-09-20 — L1 曾无手机号 pattern**：`PATTERNS` 只有身份证/密钥类，裸手机号
  `risk=0` 原文出境（生产实测）。已补 `1[3-9]\d{9}`（权重 60，单命中转本地）+ 关键词
  `手机号 20`。教训：AGENTS 写"Regex(身份证/手机…)"不等于真有，改 L1 先跑 `inspect_text`
  battery，别信文档。

- **2026-09-20 — 测试身份证须校验位合法**：`idcard` pattern 有 `idcard_valid` 校验，
  `…011234` 这类随手编的号会被静默跳过（只剩关键词分），用例须用 `…011237` 等真合法号。
  Embeddings 同理。

- **2026-09-20 — P99 口径已换**：`latency_percentiles` 改算 `gateway_internal_ms`（旧值
  含上游生成，P99 曾达 12s；现 p50≈3ms / p99≈230ms）。看历史 dashboard 曲线断崖别慌；
  `avg_ms`（统计概览）仍是总耗时，未换。

- **2026-09-20 — shell 里查 `request_log` 先 export**：`.env` 不会自动加载，
  `ADMIN_DB_URL`/`DATABASE_URL` 为空时 `AdminStore` 静默用内存后端（重启即丢，查出来全 0
  还以为没流量）。`os.environ.setdefault` 读 `.env` 前两行即可，别打印值。

- **policy.yaml 与 policy.py 必须同步部署**：`when` 条件的新运算符（如 `==`）在 `policy.py`
  的 `_eval_cond` 里实现；只传 yaml 不传 py 会**静默失效**（条件永不匹配，不报错）。P2 起
  Pydantic 强校验覆盖字段名 / action / priority / name 拼写，但 `when` 内部运算符仍是
  自由字符串解析，不会报错。

- **policy.yaml 修改后必须重启**（P2 改 fail-fast）：`load_policy()` 启动 / 每次请求用
  Pydantic 强校验；写错 action 名（不在 `allow`/`route_local`/`block`）/ priority 越界
  （0..1000）/ 缺 `name` 都会立刻抛 `ValidationError`，不再静默失效。

- `routing.yaml` 的 `default_external` / `default_local` / provider `local: true/false` /
  `policy.target` 四者不一致是常见 bug 来源；改完跑 `tests/test_gateway.py` 验证覆盖
- 大文件限 50 页 PDF / 100 行 × 20 列 Excel；文件解析在 `run_in_threadpool` 不要直接 `await`
- `policy.yaml` 的 `text matches_regex` 需用单引号包 YAML 字符串（避免 `\` 被吃）；复杂
  正则写到 `inspection.py` 而不是 `policy.yaml`
- `@app.on_event("shutdown")` 在 FastAPI 0.115+ 已 deprecate（测试 warnings 里有，可忽略）
- 流式降级触发：`prompt_too_long` / `5xx` / `breaker_open` → `vllm_local` 兜底；SSE 里会多
  一段 `chatcmpl-fallback` 事件告知
- `auth()` 强制鉴权（无 key 401；旧"已关闭/BYOK"说法已过期）：`Bearer` / `x-api-key` 两头
  都认；黑名单 deny-overrides（只看 Bearer 会被 `x-api-key` 绕过）。生产 `gateway_only` 下
  外发额度仅限具名登记身份，dev key 外发 503 fail-closed
- `requirements.txt` 固定 `fastapi 0.115.* / uvicorn 0.30.* / pydantic 2.9.*`；
  `pytesseract` 已在内（配系统 tesseract + chi_sim 即真 OCR）；`pytest` / `reportlab` /
  `paddleocr` 不在，测试或重 OCR 需单独装
- **P0 红队 (2026-09-07) — PII regex 抗 Unicode 绕过**：`inspection.inspect_text` 入口对
  数字类 PII（idcard/phone/bankcard）先做 `NFKC normalize` + 去不可见字符
  (`U+200B-F`/`U+202A-E`/`U+FEFF`/`U+2060-4`) + 去常见分隔符（`- _ . / | ,` + 全角空格）
  ⇒ `110101-1990-0101-1234`、`11010\u200b1199001011234`、全角数字全部命中。**关键词扫描
  仍跑原文本**（中文关键词不能 NFKC 掉）；其他 PII（email/AKIA/ghp_）跑 NFKC copy 不去
  分隔符。文件类回归在 `tests/test_expanded.py` 第 8 节
- **P0 红队 (2026-09-07) — 文件扩展名不可信**：`file_inspector._sniff_magic` 用文件头
  magic bytes 嗅探真实类型；用户声明 `.txt` 但内容是 PNG/PDF/XLSX 二进制 ⇒ **强制走对应
  分支**（之前走"通用文本"拿一堆 null 字节当文本，**PII 静默放行**）。`inspect_file()`
  入口 + image 路由处都有 mismatch 检查 + findings flag `ext_mismatch/magic_ext/claimed_ext`
- **P0 红队 (2026-09-07) — OCR 失败时 fail-CLOSED**：`inspect_image()` OCR 返回 `text=""`
  时设 `findings.ocr_empty_and_image=True`，policy 规则
  `ocr_empty_image_route_local (priority 12)` 命中即 `route_local`。三种 fail-OPEN 场景
  都封死：OCR 引擎没装 / 图损坏 / HEIC/AVIF 等不支持格式。测试
  `test_inspect_image_corrupted_fail_open` 已覆盖
- **P1 红队 (2026-09-08) — 会话 ID 是客户端自填的**：上传机密文件命中 route_local/block
  时给 session 打标（`gateway:session:<id>`），同 session 后续走
  `session_route_local (priority 5)`。**换 session ID / 不带 session 头可绕过** —— 曾加
  client 身份绑定标记（`gateway:client:<fp>`）堵死，但 **2026-09-18 上线前整体移除**：
  标记强制 7 天 route_local，而 route_local 计入黑名单建议引擎的"异常处置"，形成"上传
  一次 → 全本地 → 攒够 20 次 → 被建议拉黑"的误报回路。若日后恢复，须先让建议引擎排除
  `rule=session_route_local` 的计数
- **P1 红队 (2026-09-08) — XFF 不可信**：`main._resolve_client_ip()` socket IP 优先；XFF
  仅 `AI_GATEWAY_TRUST_XFF=true`（反向代理部署）时采用。`_client_ip()`（审计）与 4 处
  rate-limit 统一走它。之前报的"XFF 轮换绕限流"是误报（TestClient 伪影）
- **P1 红队 (2026-09-08) — /admin* 局域网全可读**：`admin_auth` 默认恒放行
  `10./192.168./169.254.`，生产 `.env` 未设 `AI_GATEWAY_ADMIN_IP_ALLOWLIST`，故整个办公网
  可读 `/admin/csv`（含 PII 预览）。新增 `AI_GATEWAY_ADMIN_STRICT_LAN=true` 收紧开关
  （默认 false 保持行为）；**网关纯内网，LAN 全员即用户群，STRICT_LAN 保持 false 不翻**
  （翻了办公网 dashboard 全 403）。以后要卡某台机器再把 allowlist 写死 + 翻开关
- **部署漂移事故 (2026-09-08)**：只推 `main.py + session_store.py` 而漏推它 import 的新
  符号所属文件（`metrics.py`/`redaction.py`）→ 生产 crash-loop（`ImportError: get_current_status`，restart counter 10 次后 failed）。教训：**推 main.py 前先全量对
  md5**，推后**先远端 import 再 restart**，不要依赖 systemd 重试
- **OCR 引擎缺失时 fail-CLOSED 转本地**：`inspect_image()` / 扫描件 PDF 的 OCR 返回
  `text=""` 时设 `findings.ocr_empty_and_image=True`，规则命中即 `route_local`。**缺引擎
  不再静默放行，但本地模型也看不懂图** —— 生产 `.venv` 仍要装 `pytesseract + 系统
  tesseract-ocr + chi_sim`（已进 requirements）或 `paddlepaddle + paddleocr`，否则扫描件/
  截图类请求全走本地降级。部署后用一张已知含文字的图打 `/v1/files/check`，看
  `findings.char_count > 0` 验通
- **图片支持含 heic/avif**：`file_inspector._open_heif_image` 已接（曾缺失，iPhone 截图直
  放行过）。无 OCR 引擎 / 图损坏 / 超 20MB 一律 fail-closed 转本地（`ocr_empty_image_route_local` / `unparsed_binary_route_local`），不 500 不挂起
- **图片 size cap = 20MB**：`_IMAGE_OCR_MAX_BYTES`；超大图直接 fail-open 返空 +
  `ocr_errors_total++`，不解码、不进引擎（防 OCR 炸弹）。调整需同步
  `test_inspect_image_oversize_no_ocr`
- **多页 TIFF 只 OCR 首帧**：`_ocr_image_bytes` 取 `doc[0]`；多页 TIFF（扫描仪常见输出）
  只在 `findings.char_count` 体现第一页。要支持多页需循环 `len(doc)` 累加 text 并按页码加
  `[OCR_PAGE i]` 标记（参考 PDF 路径的 5 页限制）
- **`.env` 曾入库，2026-09-29 已脱库**：`AGENTS.md` 仍入库。`.env` 自 baseline
  `6585e2b`（2026-09-18）起 tracked ⇒ 里面的凭据在 git 历史里；2026-09-29 加
  `.gitignore` + `git rm --cached .env`（磁盘文件保留，`env_file.py` 读
  `Path.cwd()/.env`、systemd `EnvironmentFile` 重读同一路径，运行零影响）。
  **将来若加远端或推到内网 git，先轮换凭据**。`routing.yaml` 永远只写 `api_key_env`
- **DeepSeek 模型名**：`deepseek-chat` / `deepseek-reasoner` 在 2026-07-24 已退役，V4 唯一
  ID 是 `deepseek-v4-flash` / `deepseek-v4-pro`；`routing.yaml` 已切到
  `api.deepseek.com/v1` + `DEEPSEEK_API_KEY`，OpenRouter slug `openrouter/deepseek-v4-flash`
- **P2 Prometheus 查询**：status 由 `main.py` 的 `_capture_response_status` 中间件写
  ContextVar 提供；测试 / 非 HTTP 路径走 `_infer_status()` 从 `action`/`rule` 推断。
  `status_code="0"` 表示未知（极少数 fallback 路径）
- **audit 里的 secret**：`log_entry()` 落 audit 之前过 `redaction.py`，AKIA / PEM /
  GitHub PAT / sk-ant- / sk-proj- / AIza / xoxb- / JWT / Authorization Bearer mask 成
  `[REDACTED:<kind>]`。**PII 不脱敏**（命中走 `route_local`，audit 留原文方便追责）

- **2026-09-23 — 文件名污染 taint（客户端无感会话替代）**：记 `(key 指纹, 小写文件名)`
  集合（`gateway:taintset:<kid>`，TTL 同 session），读做双向后缀匹配。起因：mention 正则把
  中文前缀吞进文件名（"帮我看看X.xlsx"整体命中），写是干净上传名、读是污染 mention，精确
  匹配永远对不上。policy `tainted_file_ref` priority 12。

- **2026-09-23 — BACKEND=laya 切早必炸（已修）**：`_call_laya` 主路径取的是 `l2_url()`
  （8002 OpenAI chat 口），classify 包打过去 vLLM 必 400 → 切当天全灰进 `l2_unavailable`
  （fail-closed 方向，悄悄全走本地）。已改主备一律走 `AI_GATEWAY_L2_SHADOW_URL`，为空直接
  degraded（`_l2_decision` 照样 route_local，审计留痕）。两处配置项现状：`SMALL_MODEL_URL`
  只管 qwen-chat，`SHADOW_URL` 实为 laya-classify 唯一地址（名字是历史包袱，别动）。

- **2026-09-29 — 终端跑一轮 L2 训练/晋升（配方，别现推）**：`/admin/login` **只收
  `password` 字段**（username 硬编码 admin），cookie 7 天；重新登录：
  `curl -c /tmp/ck.txt -H 'Content-Type: application/json' -X POST
  127.0.0.1:8080/admin/login -d "{\"password\":\"$AI_GATEWAY_ADMIN_PASSWORD\"}"`。
  轮询 `GET /admin/api/l2/retrain/status`（`state=done|failed` + `report` 三门，
  一版 6–7 分钟）；`POST /admin/api/l2/retrain/promote {"version":N}` 之后**必须**
  `curl -s 127.0.0.1:8003/health` 核 model 名（job 状态不算数；UI 的"晋升影子"
  按钮只按 job.state 渲染，已晋升也照挂，见 L2Page 的 candVer==vers.current 分支）。
  灌数后看本轮窗：`scripts/shadow_pulse.py --since`（裸参数读
  `var/l2-round.json` 的轮次起点，旧的 `hours` 位置参数仍兼容）。
- **2026-09-29 — 别用 `pgrep -f <脚本名> | head -1` 停临时服务**：`laya_l2_server.py` 的
  **8003 生产服务（systemd MainPID）和 8004 候选评测服务 argv 完全一样**，按命令行取
  "第一个"PID 大概率命中生产服务 → `kill -9` 直接把影子服务打死（本次事故：v29 OOD 收尾
  误杀 8003，靠 `systemctl status` 的 MainPID 对上才发现）。正确做法：起服务时
  `nohup env ... & CAND=$!` 记住 PID，收尾只 `kill $CAND`；实在要查，先
  `ss -ltnp | grep :8004` 确认端口归属再动手。
- **2026-09-29 — 判断服务活不活只认 `ss -ltnp` + 直打，别信 `systemctl list-units | grep`**：
  这台机上 vLLM 系 unit 命名不规律（`vllm.service` 一个 unit 统管 4 卡 TP，另外还有
  `vllm-bge-m3` / `vllm-qwen3-4b`），按名字 grep 很容易漏掉正在跑的服务。本次连续
  判断错三次：以为 8003 上有 MiniLM 残留（GPU5 实际只有 laya 1950MiB）、以为权威
  qwen 的 8002 没在跑（实际在跑、直打正常）、以为 laya 是权威（`.env` 里
  `AI_GATEWAY_L2_BACKEND=qwen`，laya 只是影子）。**引用会话早期结论前先复核**，
  状态类事实（谁在跑、配置是什么）一律现查现用。
- **2026-09-29 — L2 影子定版 v30，停止迭代**：`var/laya-l2.env` =
  `laya-finetuned-confidential-v30`，三门 battery 42/42 + 标注回归 206/206 + 分歧回归
  17/17（`divergences.unmatched=2` = text_preview 截断，不计分），OOD 219/220（唯一错 =
  公开招标公告误报 p=1.000，误报方向可接受），灌数首轮
  48/49（唯一分歧 = 分红明细那条稳定漏报，qwen 主判机密 + L1 已 route_local，不构成
  上线阻塞）。证据在 `var/l2-reports/v30.json`、`var/eval/laya8004-v30.log`、
  `var/l2-rounds.jsonl`（10 轮，每轮带当时影子版本）；体检入口 `scripts/l2_check.py`。
  **为什么停**：灌数单轮只有 ~49 条可比，样本量撑不起"为一条分歧训一版"（v29 就是
  这么翻车的：OOD 214、inj_prefix 漏报 6/10）。**重开条件**（满足任一才动）：
  ① 出现真实客户灰区流量攒够 ≥100 条可比；② 线上观察到 laya 相关漏判；
  ③ OOD 任一组退步。**重开时必须三门 + OOD + 灌数全跑，任一退步就留原版**，
  别只看单轮分歧数。
  **别拿 "laya OOD 219 vs qwen 178" 当切权威的理由**：同一套 OOD 上 qwen 只有
  178/220，但那 220 条是**照 laya 训练分布造的**，对 laya 有利、qwen 没在这上面训过。
  权威切换的判据是真实流量分布（见下条），不是这套自造题。
- **2026-09-29 — "等真实流量验证 L2" 在结构上不会发生**（规划陷阱）：L2 只在 L1 判灰区
  （risk ∈ [20,60)）时介入，而真实客户端内容要么 <20（L1 放行 → 直接出境）要么 ≥60
  （L1 直接 route_local）。实测 30h 内 `wb` 的 57 条请求 L2 介入 **0 条**，且全部来自
  `127.0.0.1`（本机测试，**生产真实流量 = 0**）⇒ 影子在真实流量上的样本恒为 0，
  那个 ~92% 一致率 100% 来自灌数。**真正的敞口在 L1**：L1 allow = 内容直发外部
  provider，中间没有 L2 兜底，所以该审的是"L1 有没有漏判"，不是"laya 准不准"。
  顺带：审计条目没有 key 身份（只有 `client_ip` / `token_masked`），**这不是缺口**——
  影子统计是模型对比，不需要按 key 归因；追责/计费走 `request_log`（有 `key_id` +
  `key_name`），合成流量和真实流量的区分走轮次 `since`/`until`。
