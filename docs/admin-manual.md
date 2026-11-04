# 大模型网关 · 管理员说明书

> 对应用户侧文档：`docs/user-manual.md`。内部运维细节另见 `agents/`（部署/端口/策略/坑位）。
> 铁律：**密钥明文只许出现在运行时内存和 admin 密码框**，禁止写进任何入库文件、脚本、日志。

管理台地址：`https://10.0.0.10:8080/admin/app`，管理员密码登录，会话保持 7 天。
浏览器提示证书不受信任 = 没装内网 CA：管理台「系统 → 证书安装」页有逐步指引和
「直接下载 CA 证书」按钮（`/ca.crt` 免登录），用户侧同见 `docs/user-manual.md` 1.1。

---

## 1. 总览与状态条

顶部状态条 60 秒自动刷新，五盏灯：

| 灯 | 红的含义 | 处理 |
|---|---|---|
| 外网 key | 默认外网 provider 未配网关 key，外发 503 | Provider 管理页一键配置 |
| 内网 | 展示默认内网 provider 名 | 信息展示 |
| L2 审查 | L2 总开关关了，灰区内容直放 | 模型配置 → 审查模型打开 |
| 熔断 | 有 provider 被熔断 | 点进路由检查看，或手动恢复 |
| 审计/DB | Redis/MySQL 异常或有错误堆积 | 查审计状态，先看磁盘与 DB 连通 |

---

## 2. API KEY 管理 + KEY 三档名单

- **API KEY 管理**：新增/注销 key。合法调用身份必须先登记——未登记 key 连本地路由都进不去（401）。
- **KEY 名单（黑/中/白三档）**：
  - 黑名单：直接 403；
  - 中间档：强制转内网并跳过 L1/L2 审查（内容不出境、无需审查，审计规则 `key_middle_local`）；
  - 白名单：key 95% 跳过 L2（5% 抽样），闲置 30 天的白名单会被建议收回。
  - **一个 key 只能归属一个名单**（写入侧强制，库表唯一索引）；重复登记会被 422 拒绝，换档=先移除再加。
- key 发放后提醒用户：不要进公开仓库，不要自带 `X-Upstream-Api-Key`（网关 `gateway_only` 模式会 400）。

---

## 3. 规则管理

### 3.1 L1 规则

规则 = 优先级（小者先）+ 条件（when JSON）+ 动作（allow / route_local / block）。
新增/编辑有匹配方式构造器（正则/包含任一/文件大小）+ AI 生成匹配项辅助。
删改即时生效。`default_allow` 不可删。「锁定优先级」开启的规则是重排屏障：
「一键重排优先级」不会改变它们彼此的相对位置。

### 3.2 L2 拦截配置

判定档 scope（最近用户消息 / 工具结果 / 历史消息 / 长文抽样）+ 判密阈值（confidence ≥ 阈值才算命中），
落盘 `model_policy.yaml`，热重载生效。L2 提示词（system + user 模板）可在线改，
空串=还原默认，占位符 `{filename} {sheet_names} {headers} {preview}`。
另有代码驱动的 **system diff 触发**（非配置项，无开关）：客户端自写的 system/instructions
内容变化（含首见）时，L2 按与上次已判内容的差异自动补判**改动区**（首见或改动过大时退
均匀抽样），审计 scope 列会出现 `system`；判定不可用时不记录上次内容，下个灰区请求自动重判。

### 3.3 测试区

- **L1 规则测试**：文本/文件名/大小干跑，看命中哪条规则。
- **L2 规则测试**：真实走一次 L2 判定，看 label/confidence/reason（会注明是否降级结果）。

---

## 4. 模型配置（核心页）

### 4.1 对外模型别名

`/v1/models` 的对外名 → (provider, model) 候选链。链首怎么定：**成员档位不足两种**
（如 `local-model` 单候选）= 顺序定链首，首个启用为默认；**档位 ≥2 种**（如 `Auto-AB`）=
档位定链首，顺序只影响故障切换次序和同档并列。**无移序操作**，调序=删了重加。
改名=整组重命名（旧名立即 404）；删除不可恢复。候选只能从外部候选/内网模型表里选。

- **复杂度路由（定死常开，无开关）**：每轮用户发言由 L2 内网小模型判复杂度
  （1 轻 / 2 中 / 3 重），把「够用最省」的候选提到链首；判不出来、或启用成员档位
  不足两种时按原顺序（单档位组行为与从前相同，也不发判定调用）。
  档位（1/2/3）在「编辑」弹窗逐候选设置，新增候选默认 2。
  留痕在响应 `gateway.intent = {level, ms, cached, sticky}`（审计页无此列，排障看响应体）。
- **会话档位粘性**：env `AI_GATEWAY_INTENT_STICKY_MIN`（分钟，默认 30，0=关）窗口内
  档位**只升不降**——防止同一会话中途换模型废掉上游 prompt cache；判定失败时用会话档顶上。
  存独立 Redis 键 `gateway:stickytier:<sid>`（别删，删了粘性失效但不影响路由）。
- **对外展示白名单**：env `AI_GATEWAY_PUBLIC_MODELS`（逗号分隔，**空=全量**）只收窄
  `/v1/models` 的**可发现性**，不改可路由性——没列出的名字老客户端照常能点名调用。
  现网 = `Auto-AB,local-model`：
  - `Auto-AB`：三档复杂度路由 = 内网 qwen2.5:7b（1 轻，不出境）/ deepseek-flash（2 中）/ deepseek-v4-pro（3 重）
  - `local-model`：单候选内网主力，intent=0 直连本地

### 4.2 外部候选

失败切换池：报错按 rank→价格换下一家，上下文溢出按上下文长度最小够用换。
`fallback` 总开关关掉后不再自动切换（重启恢复 env 配置）。新增候选后会自动干跑验证。

### 4.3 内部模型（降级目标登记）

| 列 | 含义 |
|---|---|
| 网址 | 仅 laya 类**非 chat 协议**模型填 `/classify` 地址；chat 模型空着，走 provider 的 base_url |
| 健康 | 点「检查」：有网址的走 classify 探针，无网址的走 provider 连通检查；只读，不耗 token |
| 启用 | **两个作用，别只当成可见性开关**：① `/v1/models` 是否展示（现网由 `AI_GATEWAY_PUBLIC_MODELS` 白名单最终决定）；② **裸内网模型名（如 `qwen2.5:7b`）钉回内网 provider 的判据**——关掉后该名字会落回默认外网 provider ⇒ **内容静默出境**。**内网 chat 模型一律保持启用**；想从列表隐藏走白名单，别关启用 |

注意：laya 没有 chat 协议，启用后会出现在 `/v1/models`，但客户端点名它会掉进默认外网 provider
发坏请求（上游 4xx）。**laya 保持停用**，只做登记。

分流模式当前 disabled（单内部模型，分流未实现）。

### 4.4 模型限流

按最终路由到的模型全局 RPM（全客户端共享桶），超限 429。裸模型名匹配任意 provider，
`provider/model` 双匹配。

### 4.5 审查模型（L2 运行配置）

- L2 总开关 / 超时 / 白名单抽样；建议引擎阈值（触发/量/误报率）。
- **模型**：下拉从内部模型登记选，**端点自动推导**（provider 展开地址 + `/chat/completions`），不用手填。
- **后端**：`qwen`（vLLM 8002） / `laya`（决策模型 8003）。切 `laya` 后模型下拉禁用、
  「端点」变手填（从登记了网址的内部模型下拉选），影子开关自动禁用（主链路本身就是 laya）。
- **影子双跑**：qwen 权威 + laya 并行打标（`shadow_*` 进审计），切 `backend=laya` 的前置验收手段。
  影子端点从下拉选；「健康检查」按钮查草稿地址（/health + /classify 探针），免保存验证。
- 保存即生效并落盘（`l2_overrides.yaml`），重启仍在。

---

## 5. Provider 管理

provider 注册表：`base_url` 支持 `${VAR:default}` 占位符（**回写必须保留原文**，
管理页展示 raw 形态是有意的）。新增/编辑/删除（删除有引用保护：默认路由/候选/别名/规则
指着它会拦下）。「Provider 健康检查」调真实 `/models`，不耗 token。
provider key 为 write-only 配置，只写不回显，ops 日志只记长度。

内网端口对照（`agents/vllm-hosts.md` 为准）：

| 端口 | 服务 | 说明 |
|---|---|---|
| 8000 | `vllm.service` | 主力本地模型（`vllm_local`） |
| 8001 | `vllm-bge-m3.service` | embedding |
| 8002 | `vllm-qwen3-4b.service` | L2 qwen（`vllm_l2`） |
| 8003 | `laya-l2.service` | L2 laya（**不是 vLLM**，禁通配操作，见下） |

---

## 6. 路由检查 / 统计 / 审计 / 运行指标

- **路由检查**：干跑（文本 + `check/chat/responses` 文件通道，codex∪workbuddy 并集），看 L1/L2
  判定与最终去向（含文件明细）。改配置后先在这里验。
- **统计可视化**：用量 + 调节建议（一键应用：拉黑/加白/去白/重排/删规则/加限流）。
- **审计日志**：Redis 热存 + MySQL 持久化（90 天）。密钥类已脱敏，PII 留原文（方便追责）。
  复制按钮在 HTTP 下走降级方案。
- **运行指标**：Prometheus 格式。延迟口径只算网关自身耗时（L1 直判 1–3ms，+L2 约 230ms，
  上游生成秒级另计）。

### 审计日志 vs 请求明细：两个库的边界

| | 审计日志（审计页） | 请求明细（明细页） |
|---|---|---|
| 回答 | 出境没有、命中哪条规则、证据链（安全判决） | 多少 token、多慢、花了多少钱（运维/计费） |
| 存储 | Redis 热窗 5000 + MySQL（90 天） | MySQL 全量（按保留期清理） |
| 写入 | 判定完成后 `log_entry`（含 block、上游失败留痕） | 中间件 fire-and-forget（所有 `/v1/*` POST，含 401、黑名单 403） |
| **model 列** | **实际应答模型**（别名在写入时解析：`Auto-AB`→`deepseek-flash` 等） | **客户端请求名**（body 原值：`Auto-AB` / `local-model`） |
| 客户端请求名 | 单独存 `requested_model` 列（审计页展开行「请求模型」次要显示） | 就是 model 列本身（模型筛选下拉的 distinct 值） |
| 独有字段 | rule / findings / L2 / 影子 / 内容预览 / 会话 | status_code / token 四元组（cached、creation）/ 延迟三段分解 |
| 页面口径 | 热窗排查 + KPI（不加 MySQL 分页/索引，模型维度走全文搜索） | MySQL 全量筛选 + 统计图表数据源 |

注意：明细库的 model 存别名**不影响计费**——`stats_billing` 聚合时经 `_real_model_label`
按落点 provider 把别名解析回实际模型再对价目表，别名走外网的行照常计价。

---

## 7. 运维：改什么、重启什么

| 改了什么 | 生效方式 |
|---|---|
| `policy.yaml`、`routing.yaml`、`model_policy.yaml`、`l2_overrides.yaml`、provider key、别名组/档位（管理台） | 热重载/即时生效，**不重启** |
| `.env`（`AI_GATEWAY_PUBLIC_MODELS` / `AI_GATEWAY_INTENT_STICKY_MIN` / 各 L2 开关等） | 改完**必须重启**（systemd `EnvironmentFile`） |
| `src/gateway/*.py` | 必须 `sudo systemctl restart gateway.service` |
| `web/admin` 前端 | `npm run build`（直写 `static/admin`），**不重启**，源码与产物同一次提交 |

重启核对三件套（sudo 静默失败时 `is-active` 仍显示 active，必须看 PID）：

```bash
sudo systemctl restart gateway.service && sleep 2 \
  && systemctl show gateway.service -p MainPID \
  && curl -sf https://127.0.0.1:8080/health
```

MainPID 必须变，否则等于没重启。生产推代码完整流程见 `agents/deploy.md`
（备份 → 传 → hash → ast 预检 → 重启 → 核对）。

### 常见故障

| 现象 | 查哪里 |
|---|---|
| 外发 503 | 状态条外网 key 灯；Provider 页补 key；`validate_environment` |
| 用户报「浏览器提示不安全 / 脚本 TLS 报错」 | 用户没装内网 CA：指到管理台「系统 → 证书安装」页或 `https://10.0.0.10:8080/ca.crt`（免登录）；SDK 客户端可只指向 CA 文件不装系统 |
| 用户脚本解析不到错误原因 | 错误体契约：4xx/5xx 看 `detail.type` + `detail.message`（401 是纯文案；黑名单 403 双写 `detail`+`error`）；速查表在用户说明书 §2 |
| 灰区内容全走本地、审计 `l2_unavailable` | L2 服务挂了（8002/8003）：fail-closed 转本地（无 fail-open 选项，安全防线不得在故障时消失） |
| 某 provider 持续失败 | 熔断页看状态，可手动 open/closed；生产阈值 2 次/30s 窗口/恢复 10s |
| L2 全走缓存、延迟 0 | `latency_ms==0` = 没真实调用（disabled/缓存/空输入），查缓存键或开关 |
| 影子数据缺失（`shadow_degraded`） | laya 挂了或 2s 短超时；主判定不受影响，只丢影子 |

### 禁止事项

- 查杀进程用具体 PID，禁 `pkill -f '<串>'`（会杀掉自己）。
- vLLM 相关操作必须带显式进程名，禁 `systemctl restart 'vllm-*'` / `killall vllm` 等通配
  （多模型共机，一次停全部，网关侧集体 502 且难排查）。
- `laya-l2.service` 不是 vLLM，走独立 venv（`~/venvs/laya`），换 checkpoint 改
  `LAYA_MODEL_PATH` + 重启该 unit 即可。
- MySQL 加列无需手写迁移（启动自动补列）；密钥相关只读 env，不进文件。
