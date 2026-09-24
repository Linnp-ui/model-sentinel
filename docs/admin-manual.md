# 大模型网关 · 管理员说明书

> 对应用户侧文档：`docs/user-manual.md`。内部运维细节另见 `agents/`（部署/端口/策略/坑位）。
> 铁律：**密钥明文只许出现在运行时内存和 admin 密码框**，禁止写进任何入库文件、脚本、日志。

管理台地址：`http://<网关IP>:8080/admin/app`，管理员密码登录，会话保持 7 天。
局域网 HTTP = 非安全上下文，复制类按钮均有降级方案，可直接用。

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

## 2. API KEY 管理 + KEY 黑白名单

- **API KEY 管理**：新增/注销 key。合法调用身份必须先登记——未登记 key 连本地路由都进不去（401）。
- **KEY 黑白名单**：黑名单 403（deny 优先）；白名单 key 95% 跳过 L2（5% 抽样），闲置 30 天的白名单会被建议收回。
- key 发放后提醒用户：不要进公开仓库，不要自带 `X-Upstream-Api-Key`（网关 `gateway_only` 模式会 400）。

---

## 3. 规则管理

### 3.1 L1 规则

规则 = 优先级（小者先）+ 条件（when JSON）+ 动作（allow / route_local / block）。
新增/编辑有匹配方式构造器（正则/包含任一/文件大小）+ AI 生成匹配项辅助。
删改即时生效。`default_allow` 不可删。

### 3.2 L2 拦截配置

判定档 scope（最近用户消息 / 工具结果 / 长文尾部）+ 判密阈值（confidence ≥ 阈值才算命中），
落盘 `model_policy.yaml`，热重载生效。L2 提示词（system + user 模板）可在线改，
空串=还原默认，占位符 `{filename} {sheet_names} {headers} {preview}`。

### 3.3 测试区

- **L1 规则测试**：文本/文件名/大小干跑，看命中哪条规则。
- **L2 规则测试**：真实走一次 L2 判定，看 label/confidence/reason（会注明是否降级结果）。

---

## 4. 模型配置（核心页）

### 4.1 对外模型别名

`/v1/models` 的对外名 → (provider, model) 候选链，顺序即优先级，首个为默认。
改名=整组重命名（旧名立即 404）；删除不可恢复。候选只能从外部候选/内网模型表里选。

### 4.2 外部候选

失败切换池：报错按 rank→价格换下一家，上下文溢出按上下文长度最小够用换。
`fallback` 总开关关掉后不再自动切换（重启恢复 env 配置）。新增候选后会自动干跑验证。

### 4.3 内部模型（降级目标登记）

| 列 | 含义 |
|---|---|
| 网址 | 仅 laya 类**非 chat 协议**模型填 `/classify` 地址；chat 模型空着，走 provider 的 base_url |
| 健康 | 点「检查」：有网址的走 classify 探针，无网址的走 provider 连通检查；只读，不耗 token |
| 启用 | 只控制在 `/v1/models` 里出不出现——开则客户端可见可点，关则仅登记（审查下拉/别名候选里照常可选） |

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
| 8000 | `vllm-qwen3-8b.service` | 主力本地模型（`vllm_local`） |
| 8001 | embedding | `bge_m3` |
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

---

## 7. 运维：改什么、重启什么

| 改了什么 | 生效方式 |
|---|---|
| `policy.yaml`、`routing.yaml`、`model_policy.yaml`、`l2_overrides.yaml`、provider key | 热重载/即时生效，**不重启** |
| `src/gateway/*.py` | 必须 `sudo systemctl restart gateway.service` |
| `web/admin` 前端 | `npm run build`（直写 `static/admin`），**不重启**，源码与产物同一次提交 |

重启核对三件套（sudo 静默失败时 `is-active` 仍显示 active，必须看 PID）：

```bash
sudo systemctl restart gateway.service && sleep 2 \
  && systemctl show gateway.service -p MainPID \
  && curl -sf http://127.0.0.1:8080/health
```

MainPID 必须变，否则等于没重启。生产推代码完整流程见 `agents/deploy.md`
（备份 → 传 → hash → ast 预检 → 重启 → 核对）。

### 常见故障

| 现象 | 查哪里 |
|---|---|
| 外发 503 | 状态条外网 key 灯；Provider 页补 key；`validate_environment` |
| 灰区内容全走本地、审计 `l2_unavailable` | L2 服务挂了（8002/8003），`AI_GATEWAY_L2_FAIL_MODE=open` 会改成放行（默认降本地） |
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
