# HTTPS 证书安装（客户端一次性）

网关已切到 HTTPS（端口 8080）。浏览器首次访问会提示"不安全/证书不受信任"，
是因为网关使用**内网自签 CA**，需要把 CA 装进本机信任库一次即可。

> **管理台里也有这份说明**：控制台「系统 → 证书安装」页（`web/admin/src/components/CertGuidePage.jsx`）
> 是这份文档的网页版：各平台步骤相同，页面多一个「直接下载 CA 证书」按钮并自动填好
> 本机真实地址（不用手填 IP）；下面「一键下载 CA」一节只有文档有。控制台要管理员密码
> （证书下载本身 `/ca.crt` 免登录）。改文档的步骤时顺手改那个页面。

## 一键下载 CA

浏览器打开（会先弹"不安全"，点"高级 → 继续前往"即可下载）：

```
https://<网关IP>:8080/ca.crt
```

或命令行：

```bash
curl -k -o gateway-internal-ca.crt https://<网关IP>:8080/ca.crt
```

## Windows（Edge / Chrome）

1. 双击下载的 `gateway-internal-ca.crt`
2. "安装证书" → "当前用户" → "将所有的证书放入下列存储"
3. "浏览" → 选"受信任的根证书颁发机构" → 确定 → 完成
4. 弹出安全警告点"是"
5. **重启浏览器**，再打开 `https://<网关IP>:8080/admin/app` → 不再提示

命令行方式（PowerShell，点弹窗"是"）：

```powershell
certutil -user -addstore Root gateway-internal-ca.crt
```

## macOS

双击 `.crt` → 钥匙串访问 → "系统" → 找到 "Gateway Internal CA" → 双击 → "信任" → "始终信任"。

## Linux

```bash
sudo cp gateway-internal-ca.crt /usr/local/share/ca-certificates/gateway-internal-ca.crt
sudo update-ca-certificates
```

## Firefox（不走系统证书库）

设置 → 隐私与安全 → 证书 → 查看证书 → "证书颁发机构" → 导入 → 选文件 → 勾"信任此 CA 以标识网站"。

## 命令行客户端

- curl：`curl --cacert gateway-internal-ca.crt https://<网关IP>:8080/health`（`--cacert` 仅未装 CA 时需要；`/health` 免 key）
- Python/requests：`REQUESTS_CA_BUNDLE=gateway-internal-ca.crt`
- Node：`NODE_EXTRA_CA_CERTS=gateway-internal-ca.crt`
- Go (codex/reqwest)：`SSL_CERT_FILE=gateway-internal-ca.crt`

装完 CA 后以上都不需要额外参数。

## 常见问题

- **装了还是提示不安全**：关闭所有浏览器窗口重开；或确认装到了"受信任的根"而非"中间"。
- **curl 报 CRYPT_E_NO_REVOCATION_CHECK**：已修复（网关提供 `/ca.crl`），升级到最新；老版本用 `curl --ssl-no-revoke`。
- **证书过期/换 IP**：联系管理员重签（SAN 列表见 `agents/deploy.md`）。