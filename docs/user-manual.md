# 大模型网关 · 用户说明书

> 内网统一出口：你的请求先过内容审查，再发往外网大模型或内网本地模型。
> 一句话承诺：**凡是判为机密的内容，不出境**——由内网本地模型回答，或直接拦截。

---

## 1. 快速开始

### 1.1 安装 HTTPS 证书

网关用**内网自签 CA** 发证书。把 CA 装进本机信任库一次后：浏览器不再提示「不安全」，
curl/SDK 的 TLS 校验也能正常通过。不装也能调（`curl -k`），但别长期使用不安全模式。

**下载证书**：

```bash
# 浏览器打开（首次会提示不安全，「高级 → 继续前往」即可下载）
https://10.0.0.10:8080/ca.crt
```

**安装**：

| 平台 | 装法 |
|---|---|
| Windows | 双击 `.crt` → 安装证书 → 当前用户 → 受信任的根证书颁发机构；或 PowerShell `certutil -user -addstore Root gateway-internal-ca.crt`。**装完重启浏览器** |
| macOS | 双击 → 钥匙串访问 → 系统 → 找到 `Gateway Internal CA` → 信任 → 始终信任 |
| Linux | `sudo cp gateway-internal-ca.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates` |
| Firefox | 不走系统证书库：设置 → 隐私与安全 → 证书 → 查看证书 → 导入，勾「信任此 CA 以标识网站」 |
| 命令行客户端 | `curl --cacert gateway-internal-ca.crt`；Python `REQUESTS_CA_BUNDLE=`、Node `NODE_EXTRA_CA_CERTS=`、Go `SSL_CERT_FILE=` 指向该文件即可 |

### 1.2 获取 key

key 由管理员登记发放（未登记的 key 直接 401，连本地模型也调不了）。登记用的就是
**公司原本分发的 DeepSeek API key**（你直连 DeepSeek 时用的那个 `sk-...`），找管理员
登记即可；**没有 DeepSeek key 的，向管理员（张鹏）申请**。key 只能走公司额度，
不要写进公开仓库、不要发给外部人员。

---

## 2. 鉴权

- 两种带 key 方式都认：`Authorization: Bearer <key>` 或 `x-api-key: <key>`。
- `/v1/` 下所有接口都必须带 key。

| 现象 | 含义 | 怎么办 |
|---|---|---|
| 401 | 无 key / 未登记的 key | 检查 key 是否填对，找管理员登记 |
| 403 黑名单 | key 被拉黑 | 找管理员 |
| 403 policy_violation | 内容被安全策略拦截 | 改内容重发，不要反复重试同样内容 |
| 429 + `Retry-After` | 触发限流 | 按 `Retry-After` 秒数退避重试 |
| 503 | 网关侧无可用 key/模型 | 稍后重试，不行找管理员 |

**怎么读返回体**（一眼判断发生了什么）：

- **成功（200）**：顶层 `model` = 实际回答你的模型（不是别名）；`gateway.action`（`allow`=放行 / `route_local`=转本地）+ `gateway.local`（`true`=内网模型回答，内容未出境）+ `gateway.policy_rule`（命中哪条规则）就是全部关键信息。
- **失败（4xx/5xx）**：看 `detail.type`（如 `rate_limited` / `key_blocked` / `provider_not_configured`）+ `detail.message` 中文说明；429 另有 `Retry-After` 头。
- **两个例外**：401 的 `detail` 是纯文案（没有 type）；黑名单 403 同时带 `error` 字段（OpenAI SDK 兼容，读哪个都一样）。

---

## 3. FAQ

**Q：回答怎么突然变“笨”了？**
A：被转到本地模型了（内容涉密）。换不涉密的问法，或确认该内容是否真有必要外发。

**Q：403 policy_violation 里的 rule 名是什么意思？**
A：命中的规则名，方便定位是哪类内容触发。规则名对不上业务？找管理员核对。

**Q：文件传不上去？**
A：先调 `/v1/files/check` 看结论；确认扩展名真实（网关按文件魔数识别，改后缀没用）。
