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
ssh <user>@<gateway-host> "sudo systemctl restart gateway.service && sleep 2 && systemctl show gateway.service -p MainPID && curl -sf http://127.0.0.1:8080/health"
```

> 推 `main.py` 前必须先全量对 md5，把它 import 的新符号所属文件一起推；
> 推后**先远端 import 再 restart**，不要依赖 systemd 重试。
> （2026-09-08 部署漂移事故见 `gotchas.md`）

> **SSE 长连接拖住 stop 阶段（2026-10-08 实测）**：管理台状态条 SSE
> （`/admin/api/status/stream`）挂着浏览器连接时，uvicorn 优雅退出不完成，systemd
> 等满 `TimeoutStopSec=90s` 才 SIGKILL，restart 总耗时 ~95s。重启前关管理台标签页；
> 关不了就给 restart 命令留 120s+ 超时，`deactivating` 是正常等待**不是重启失败**，
> 别中途再 restart / 杀 PID。
