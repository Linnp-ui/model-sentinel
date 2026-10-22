# 部署到生产

> 上级：`AGENTS.md`。生产与本机都是 Linux，`.py` 即传即跑、无需解包。
> 流程：远端备份 → 传文件 → hash 校验 → 远端 `ast` 语法预检 → 重启 →
> **MainPID 变更核对**（sudo 静默失败时 `is-active` 仍显示 active，必须看 PID）
> → `/health` 冒烟：

```bash
ssh <user>@<gateway-host> "cp $HOME/gateway/src/gateway/xxx.py $HOME/gateway/src/gateway/xxx.py.bak_$(date +%Y%m%d_%H%M%S)"
scp src/gateway/xxx.py <user>@<gateway-host>:$HOME/gateway/src/gateway/xxx.py
ssh <user>@<gateway-host> "sha256sum $HOME/gateway/src/gateway/xxx.py"   # 对 hash
ssh <user>@<gateway-host> "cd $HOME/gateway && .venv/bin/python -c \"import ast; ast.parse(open('src/gateway/xxx.py', encoding='utf-8').read())\""
ssh <user>@<gateway-host> "sudo systemctl restart gateway.service && sleep 2 && systemctl show gateway.service -p MainPID && curl -sf https://127.0.0.1:8080/health"
```

> 推 `main.py` 前必须先全量对 md5，把它 import 的新符号所属文件一起推；
> 推后**先远端 import 再 restart**，不要依赖 systemd 重试。
> （2026-09-08 部署漂移事故见 `gotchas.md`）

> **HTTPS（uvicorn 直接终结 TLS）**：生产走 systemd 时用 drop-in
> `/etc/systemd/system/gateway.service.d/tls.conf` 加 `--ssl-certfile/--ssl-keyfile`：
> - 证书/私钥在 **`~/.config/gateway/tls/`**（`ca.crt` / `ca.key` / `server.crt` / `server.key`，
>   **仓库外**，私钥 0600）；内部 CA 装进系统信任库
>   （`/usr/local/share/ca-certificates/gateway-internal-ca.crt` + `update-ca-certificates`）
>   ⇒ 本机 curl / python / 脚本**免 -k** 直接验证通过。
> - 证书 SAN 需含实际访问 IP + `127.0.0.1` / `localhost`；**换机或改 IP 要重签**（命令见本节末）。
> - **明文 http 已关闭**（8080 只讲 TLS，没有旁路）⇒ 客户端改 `https://网关IP:8080`
>   并信任 `ca.crt`（codex/reqwest：`SSL_CERT_FILE=<ca.crt>`；Node：`NODE_EXTRA_CA_CERTS=<ca.crt>`）；
>   客户端安装步骤见 `docs/https-cert-install.md`。
> - **吊销检查**：CA/服务器证书都带
>   `crlDistributionPoints = URI:https://<网关IP>:8080/ca.crl`，网关 `GET /ca.crl`
>   （`main.py` 的 `ca_crl` 路由）发空 CRL（`~/.config/gateway/tls/ca.crl`）。
>   **缺 CDP 时 Windows/schannel 报 `CRYPT_E_NO_REVOCATION_CHECK (0x80092012)`**
>   （curl.exe / .NET 硬失败；OpenSSL 与浏览器软失败）。重生成 CRL：
>   `cd ~/.config/gateway/tls && openssl ca -gencrl -config ca.cnf -out ca.crl`。
> - **回滚**：删 `tls.conf` + `systemctl daemon-reload && systemctl restart gateway.service`
>   （回到明文 http，客户端不用改）。
> - 重签证书：`cd ~/.config/gateway/tls`，先备好 CSR（缺则 `openssl req -new -key server.key
>   -out server.csr -config server.cnf`），再 `openssl x509 -req -in server.csr -CA ca.crt
>   -CAkey ca.key -CAcreateserial -out server.crt -days 825 -sha256 -extfile server.cnf
>   -extensions ext`，最后 `systemctl restart gateway.service`（换 SAN/CDP 先改 `server.cnf`）。

> **SSE 长连接拖住 stop 阶段（2026-10-08 实测）**：管理台状态条 SSE
> （`/admin/api/status/stream`）挂着浏览器连接时，uvicorn 优雅退出不完成，systemd
> 等满 `TimeoutStopSec=90s` 才 SIGKILL，restart 总耗时 ~95s。重启前关管理台标签页；
> 关不了就给 restart 命令留 120s+ 超时，`deactivating` 是正常等待**不是重启失败**，
> 别中途再 restart / 杀 PID。
