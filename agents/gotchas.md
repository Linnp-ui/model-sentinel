# Gotchas（坑位记录）

> 上级：`AGENTS.md`。按时间倒序读也行，按主题扫也行。

- **2026-09-24 — 待办：影子 v9 新基线回看（24–48h 后，换 session 先读这条）**：
  v9 + 渲染修复上线（laya-l2 MainPID 3065585，权威仍 qwen）。到点重跑
  `GET /admin/api/l2-shadow-stats?hours=168`（admin 鉴权）。口径：cells
  cc=主密影密 / cn=主密影放（影子漏报）/ nc=主放影密（影子误报）/ nn=主放影放；
  覆盖率=shadowed/l2_total；一致率=(cc+nn)/compared（main_degraded 不计入）。
  **对比基线注意**：此前 168h 窗数据被 v8+空前缀伪影污染（nc=19/28、灰区探针
  18/24 判密全是放大器产物），新数据从本次服务切换起算，不能直接比绝对值，
  看方向：① 覆盖率应 ≥95%（_carry_shadow 修复后 27/28）；② nc 占比应显著降
  （v9 离线+在线验：CAP/涨薪/邮件改写全 0.0，工资表/mention 机密 1.0；灰区闲聊
  4 话术剥前缀 v8 4/4 误报→v9 0/4。注意 laya 是 argmax 确定性推理，探针带
  `灰区B{n}:` 这类人工 ID 前缀会扰动输出=测试假象，复测用裸话术）；
  ③ 抽 nc/cn 分歧样本原文 = v10 训练原料。**v10 触发条件**：分歧样本攒够
  或一致率仍 <50%——届时补 商务邮件+流水号 负样本（现残留 1.0）+ 2 条退化
  样本标注（重复样板×4、200 字截断英文样板）。在线 38 条 battery 复测
  34/38 与离线一致：探针 8/8、真实 23 为 19/23（TP11 FP2 FN2 TN8）、回归 7/7；
  4 错例已知：FP=session@E:\task（含机密文件名但标注 default_allow，标注口径
  问题）+ 商务邮件流水号，FN=两条退化样本。复测脚本 `/tmp/opencode/live_eval.py`
  （打 8003 直测，渲染与离线 `eval_laya_confidential.render` 逐字一致）。
  **测试要审计落地**：进影子基线的 eval 流量必须经网关（`dev-key` +
  `vllm_local/qwen2.5:7b` 斜杠写法，L1→L2 主影双落审计）；直打 8003 只算
  模型 QA，不进 request_log/audit/影子统计窗。09-24 灰区 6 条经网关复测已落地
  （3 闲聊 allow 主影双 NORMAL、1 涨薪话术主 qwen 判密 route_local/影 NORMAL=真实
  分歧样本、mention route_local 主影双 CONFIDENTIAL、良性技术 L1 直判无 L2）。
  09-24 灰区灌数 56 条（`scripts/gray_flood.py`：inspect_text 预筛 [20,60)
  → 网关 → 审计对账）：v9 影子 56/56 命中预期（闲聊 36 全 NORMAL、payload/mention
  20 全 CONFIDENTIAL，0 误报 0 漏报）；1h 窗分歧结构：cn=9 主 qwen 薪资闲聊过判密
  /nc=4 主放影密 v9 反超。
  **查询坑**：`request_log`/`audit_logs` 的 `ts` 存 UTC，MySQL `NOW()` 是
  北京时间（UTC+8）——ad-hoc SQL 用 `ts > NOW() - INTERVAL n` 恒空，
  cutoff 用 Python `utcnow()` 或 `NOW() - INTERVAL 8 HOUR`
- **2026-09-24 — v9 切影子 + 服务渲染空前缀伪影（重要）**：laya 影子服务切 v9
  （mention-only 正 360/负 360 + 正式中文镜像 160/160 进训练，6696 条）。真实 23
  条 v8/v9 同分 19/23 错集不同：v9 修 mention-only FN（生产 codex Files-mentioned
  真形态），回退两条退化样本（重复样板×4/截断样板）。**空前缀伪影**：服务
  `_render_state` 恒渲染 `文件名: \nSheet: \n表头: \n`（空值），同 checkpoint
  同文本 v8 CAP 从评测格式 0.013 抬到服务格式 0.96——灰区误报放大器，之前
  "影子 18/24 判密"被它放大。已改空字段不渲染（与训练/评测布局一致），重启
  laya-l2（非网关）。线上验：CAP/涨薪闲聊 1.0→0.0，工资表 1.0，mention 机密
  1.0/良性 0.0；残留：商务邮件+流水号后缀 1.0（下轮补"编号后缀"负样本）。
  权威仍 qwen，v9 继续影子攒数；回退链 v8→v5→v4→零样本
- **2026-09-24 — 影子覆盖率 3/151 是统计口径被低估（已修）**：`classify_chunks`
  三个返回分支自建 `out`，丢了各块跑出来的 `shadow_*`——生产 L2 大都是多
  scope（codex/tool），只有单块 chat 留痕。修：`_carry_shadow` 把影子键从
  决策块带出来（命中块/首个有影子块/best 块）。线上验：tool 双块探针
  chunks=2 主影双全。历史审计行补不回来，覆盖率从修复点起算
- **2026-09-24 — v8 现服务留出实测（网关 0.7 切）73.9%，不切 backend=laya**：
  直打 8003 现服务 23 条：TP11/FP4/**FN2**/TN6。FN 全是"文件名提及无 payload"
  （`local_financial_salary.csv` / `生产过程问题记录追踪表…`，p=0.000 自信放行）
  ——生产 L1 有 taint/会话兜底，但 L2 权威必须自己站得住；FP 4/12 含 CAP 定理
  0.96/商务邮件改写 1.0（中文正式感误高家族，没根治）。此前说的 82.6% 是
  server 0.5 切（19/23），网关 `is_confidential` 用 label+conf≥0.7，决策以
  73.9% 为准。线上影子 n=3 无信号。保持 qwen 权威+影子攒数；下轮训练加
  mention-only 正样本 + 普通中文技术/商务负样本
  同日线上验证：24 发灰区探针（涨薪/年终奖/绩效/分红话术，risk 20）经
  `vllm_local/qwen2.5:7b` 斜杠写法过 L1→L2（qwen 全 NORMAL）→allow 落本地，
  零出境；laya 影子 18/24 判 CONFIDENTIAL（conf 1.0）——普通薪资闲聊都判密，
  不切的决定在线上复现了。另：`local-model` 别名走 local_alias_bypass 跳过
  L1/L2（灰区探针灌它测不到 L2）；`qwen2.5:7b` 裸名直写会落 default_external
  出境（已实测一发良性），灰区压测只许用 `vllm_local/` 斜杠写法
- **2026-09-24 — revert 必须整块（半截 revert 出过 500）**：`metrics_timeseries`
  切方案时只恢复了函数签名、留下三行切片/抽稀引用未定义 `hours`/`RING_POINTS`，
  线上 `/admin/metrics/timeseries` 全 500（总览/运行指标双双刷不出）。教训：
  revert 按 diff 块整个消，签名和函数体分两次改必留半截；改完先本地复调一遍
  端点再重启（本次靠用户控制台报错才发现，测试无此端点覆盖）
- **2026-09-24 — `CircuitBreaker.state()` 读即迁移**：OPEN 且冷却到时，
  调 `state()`/`allow()` 当场迁 half_open 再返回——RECOVERY=0 时 `state()`
  永远报不出 open（刚重开也被读回 half_open）。看快照/写单测断言重开时注意；
  别名 503 信封曾误标：`resolve()` 预钉默认 provider（deepseek），别名层
  `no available candidate` 抛错时信封沿用旧值。已修：`RoutingError.provider`
  字段（别名抛点填别名名）+ `_err_provider(exc, prov)` 10 处信封统一优先；
  审计侧 `provider` 仍是预解析值（`requested_model` 里有真别名，不动）

- **2026-09-24 — 管理台禁用 `sticky`，统一真 fixed + 实测让位**：`sticky` 在 body
  滚动 + flex 下每帧重算位置，会抖（侧边栏/统计工具条/顶栏三连中招）。现统一：
  Sider `position:fixed`（`marginLeft:200` 让位）；顶栏（状态条在上+标题在下）
  包 `.app-topbar` 真 fixed（`left:200px`），高度 ResizeObserver 实测后内容区
  `paddingTop` 让位——状态条窄屏换行增高自动跟。页面工具栏不再跟内容滚动：
  标题右插槽 `#page-toolbar-slot`（App.jsx Header 内 `marginLeft:auto`），各页用
  `createPortal` 把控件挂进去，随页卸载自动清空（StatsPage 范式）。`theme.css`
  只剩 `.app-topbar` 一条定位规则，别再加逐行 sticky 偏移
- **2026-09-22 — Laya 影子期：零样本探针结论与误报监控**：Laya（`laya-l2.service` 8003）
  是非自回归决策模型（无 OpenAI 协议，vLLM 跑不了）。2026-09-22 离线探针（8 种
  题规格 × 8 样本）：**noul + 英文题**是最优规格——工资表/施工图/身份证/报价单等
  机密样本 0.76-1.0 全中，但**普通中文技术文档/讨论会误高**（0.63-0.91，与真阳性
  区间重叠，单靠阈值分不开）；中文 choice 题则整体偏弱（工资表仅 0.35）。
  影子期（`AI_GATEWAY_L2_SHADOW=true`，qwen 权威不变）核心看两数：
  ① qwen=NORMAL 且 shadow_conf≥0.7 的**误报率**（技术类内容重灾区）
  ② qwen=CONFIDENTIAL 且 shadow<0.5 的**漏报率**。误报率不可接受 → 微调
  （审计库即标注语料，RLCD notebook 在模型仓库）或放弃切换；达标才
  `AI_GATEWAY_L2_BACKEND=laya`。另：根目录 checkpoint 只认英文（非拉丁脚本
  "准确率0@置信度0.95"），必须用 `multilingual` 子目录；出厂 temperature 未拟合
  （全 1.0），概率可信前需在自己数据上 refit
  **同日已完成微调两轮**：审计库去重后仅 73 条真实样本（97% 是 agent 重复请求），
  用 `scripts/build_laya_dataset.py`（审计抽取+模板槽位合成，全本地）
  + `scripts/laya_finetune_local.py`（官方 DDP notebook 适配单卡，~2min/4ep）：
  - v1（2250 条）：8 例探针 50%→100%，真实留出 23 条 52%→78%（FP 9→2），
    温度拟合 4.208；但模拟评测（`scripts/eval_laya_simulated.py`，264 条独立
    种子）暴露"**公开数字**"缺口——年报/公开价/中标公告/JD 薪资范围误判（17%）
  - v2（+公开数字负样本，`...-confidential-v2`，温度 0.987）：模拟 264 条
    **100%**，真实留出 **83%**，概率饱和双峰（1.000/0.000）
  服务跑 v2（unit `LAYA_MODEL_PATH`；回退链 v1 `.../laya-finetuned-confidential`
  → 零样本 `.../laya/multilingual`）。剩余已知弱项：session 包装上下文误报、
  疑似 Qwen 误标的设备手册。重训先 `build_laya_dataset.py` 再 `laya_finetune_local.py`。
  **数据集/checkpoint 含机密内容，留在 ~/models 不进仓库**
- **2026-09-23 — L2 门取消字数触发 + 安全矩阵 + v3/v4 + L1 补词**：
  `main._review_text` 只看灰区 `[20,60)`（`_l2_feed` 死变量已删）；干跑三处
  （admin_api）`trigger` 只剩 gray；测试 3 处改灰区文本触发 + 新增短灰进 L2 用例。
  安全矩阵 160 条（`build_chat_workdoc_testset.py`，10 场景×8 问法×机密/放行，
  纯合成可入库）+ `eval_security_testset.py`（镜像新门）。终值 **conf 89% / pass 99%**。
  v3（+英文混合/预算话术/短聊天/拼音/密钥，3750 条）模拟集 100%→92%（合同纪要 38%：
  v3 会议类负样本把"会议语境"推向 benign，而 v1 会议正样本全带"不得外传"强标记→
  学出"无标记=安全"错规则）；v4 加对称正样本（无标记正式纪要/试验报告/正式通知
  各 150）后模拟 97%、矩阵 89%/99%。v4 数据有意外：组装 bug 致 base 模板跑两遍
  （5520 条，base×2，合法加倍故保留 checkpoint，脚本已修）。v4 例会纪要阴性 62%
  但全 risk 0、新门下到不了 L2，生产无关。服务切 v4（MainPID 1935514），网关重启
  （MainPID 1936503）。L1 补 22 词（全 20 档）：涨薪/年终/绩效/分红/salary/bonus/
  revenue/package/offer/headcount/配筋/开挖/切削/HRB/通讯录/花名册/电话/别外传/
  密钥/token/password/AKIA（英文仅小写形态）。残留：无信号隐晦句（risk 0 到不了 L2）、
  高管行程（question 规格未覆盖）、"端午福利 411 元"（L2 0.53）、唯一 FP
  "工资发放流程说明"（L2 1.0）。探针 100% / 真实 74% / 模拟 97%（Brier 0.034）。
  生成器校验位 bug：`rnd_id_card`/`rnd_bank_card` 公式错（0/20 有效），已按
  inspection 修好；生产 inspection 是对的，v1–v4 训练用 PII 校验位无效但对 L2
  语义无影响，不重训。另修测试集生成器年终奖 `%`/`*` 优先级 bug。
- **2026-09-24 — v5（当前服务）**：数据集重建（真实仍 50+23，合成正 2570/负 1580，
  共 4200 条，GPU6 约 12min/4ep，温度拟合 [1.2,1.2,1.0]）。真实留出 23 条：
  v5 acc 73.9% 与 v4 持平，FPR 0.20→0.10（FP 2→1），recall 0.69→0.62（各差 1 条，
  n=23 噪声级）。影子是非权威链，取低误报方向切 v5（MainPID 2741160）。
  回退链：v4 → 零样本 `.../laya/multilingual`。
- **2026-09-24 — TorchSight 独立评测（`~/models/public-datasets/torchsight`，Apache-2.0，
  评测 1000+500，生产同形 render，text 截 1200；7 分类按我方口径转二分类，
  malicious 政策外单列）**：v5 主分 78.7%（v4 81.2%），credentials/financial 100%、
  confidential 99%、pii 95.5%、medical 83.5%、safe 62.1%。safe 誤報集中在
  safe.config（空值密钥模板）与点名会议纪要——已知"技术文档误高"同族。
  关键发现（swap 实验证实）：同 SSN+薪资 payload，仅评语
  "Below Expectations"→"Needs Improvement" 一词之差即可让结论 0.00→0.84，
  换名无影响——**模型被修饰词劫持，而非看 payload**。下轮训练必须加
  "同 payload × 不同修饰"对称样本，逼模型锚定 payload；backend=laya 切换暂缓，
  继续影子攒数。
- **2026-09-24 — v6/v7/v8 三轮（当前服务 v8）**：v6 加中英对比对（同 payload 多包装
  全标密 720、同包装去 payload 标放 240）：swap 原样本 0.00→0.78 修好，
  safe 62%→84%，但 pii 95.5%→82.6%、medical 83.5%→47.5%——镜像负样本把
  "SSN/薪资在场"学成了必要条件。v7 加长表正样本（8-14 行联系表/通讯录/截断模拟，
  196+80）：长表 12 行 0.09→1.0 修好，pii 回到 97.4%，但 medical 只回到 61.5%。
  v8 加临床叙事（带患者标识符诊疗记录→密 120、去标识化教学→放 60）+正式财务预览/
  律师函 40：真实 23 条 **82.6% 历史最佳**，TorchSight 主分 80.7%（v4 81.2/
  v7 79.3/v5 78.7），pii 98.7%，medical 回到 83.5%，swap/长表探针全过；
  代价英文 safe 44.5%（"正式感→密"回潮）。结论：小决策模型打地鼠——每轮修好目标
  切片必动别处；中文生产分布以真实留出+影子在线一致率为准，切 v8（MainPID 2801105），
  回退链 v5→v4→零样本，不行就回。
- **2026-09-21 — 一键 apply 改了生产的 `policy.yaml` / `model_policy.yaml` 必须顺手提交**：
  建议引擎 reorder/add_limit 落盘的就是入库配置文件，不提交会让「仓库版」与「生产版」
  漂移（2026-09-21：规则 4 一键重排后 policy.yaml 挂 M 好几天，差点被回滚操作覆盖）。
  apply 成功后 `git diff policy.yaml model_policy.yaml` 看一眼，确认预期就提交
- **2026-09-21 — `upsert_policy` 更新必须传 `original_name`**：不传按新建走唯一性检查，
  直接 422 `rule name already exists`（suggestions/apply reorder 上线当天踩中）。
  更新接口（`PUT /policies/{name}`）已传；新增 apply 分支调 `upsert_policy` 改 priority
  时照抄该写法
- **2026-09-21 — 测试共用 memory store，残留行会抢 Top 位**：同 session 内各用例 seed
  的行全在一张表里。按 Top1/总量断言的用例（规则 7 加限流）必须 seed 唯一 label 且播足量
  （100 行压住残留），断言按建议 id/type 过滤、按"已配的不再出现"收敛，不按"建议消失"
  （下一个热点顶上来是正常行为）
- **2026-09-21 — SuggestionCard 原样转发 `s.apply`**：前端 `JSON.stringify(s.apply)`
  直发，后端 `SuggestionApplyReq` 字段名必须与引擎产出的 apply key 一致
  （`limit_model/limit_rpm`，不是 `model/rpm`）。加新 kind 先对齐两端命名
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
  标签、request_log `/admin/api/logs` 的 `ts` 与 `/stats/billing` 按天日期桶（经
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
- **2026-09-23 — 文件名污染 taint（客户端无感会话替代）**：记 `(key 指纹, 小写文件名)`
  集合（`gateway:taintset:<kid>`，TTL 同 session），读做双向后缀匹配。起因：
  mention 正则把中文前缀吞进文件名（"帮我看看X.xlsx"整体命中），写是干净上传名、
  读是污染 mention，精确匹配永远对不上（调试实测写成功读 miss 定位）。policy
  `tainted_file_ref` priority 12。测试 2 个（端到端无头强制本地 + store 单测）。
  全量 89 passed。网关已重启（MainPID 2040181，policy_count 9→10）。
- **2026-09-23 — BACKEND=laya 切早必炸（已修）**：`_call_laya` 主路径取的是
  `l2_url()`（8002 OpenAI chat 口），classify 包打过去 vLLM 必 400 → 切当天全灰
  进 `l2_unavailable`（fail-closed 方向，悄悄全走本地）。已改主备一律走
  `AI_GATEWAY_L2_SHADOW_URL`，为空直接 degraded（`_l2_decision` 照样 route_local，
  审计留痕）。两处配置项现状：SMALL_MODEL_URL 只管 qwen-chat，SHADOW_URL
  实为 laya-classify 唯一地址（名字是历史包袱，别动）。
