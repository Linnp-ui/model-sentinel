# 部署到生产

> 上级：`AGENTS.md`。生产与本机都是 Linux，`.py` 即传即跑、无需解包。
> 流程：远端备份 → 传文件 → hash 校验 → 远端 `ast` 语法预检 → 重启 →
> **MainPID 变更核对**（sudo 静默失败时 `is-active` 仍显示 active，必须看 PID）
> → `/health` 冒烟：

```bash
HOST=<user>@<gateway-host>; REPO=~/gateway
ssh $HOST "cp $REPO/src/gateway/xxx.py $REPO/src/gateway/xxx.py.bak_$(date +%Y%m%d_%H%M%S)"
scp src/gateway/xxx.py $HOST:$REPO/src/gateway/xxx.py
ssh $HOST "sha256sum $REPO/src/gateway/xxx.py"   # 对 hash
ssh $HOST "cd $REPO && .venv/bin/python -c \"import ast; ast.parse(open('src/gateway/xxx.py', encoding='utf-8').read())\""
ssh $HOST "sudo systemctl restart gateway.service && sleep 2 && systemctl show gateway.service -p MainPID && curl -sf http://127.0.0.1:8080/health"
```

> 推 `main.py` 前必须先全量对 md5，把它 import 的新符号所属文件一起推；
> 推后**先远端 import 再 restart**，不要依赖 systemd 重试。
> （2026-09-08 部署漂移事故见 `gotchas.md`）
