#!/usr/bin/env bash
# 一键部署（防手敲漏步）：
#   scripts/deploy.sh src/gateway/xxx.py [src/gateway/yyy.py ...]
# ast 预检 → 时间戳备份 → 重启前 MainPID → 重启 → 重启后 MainPID（必须变更）
# → is-active → /health → 前端 chunk 核对（缺产物即失败）。
# 成功即删备份，任一步失败即停且保留备份。
# 注意：改 main.py 时把它 import 的新符号所属文件也要一起传进来（否则重启后 import 失败）。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

PASS=~/.config/opencode/.sudo_pass
mainpid() { sudo -S -p "" systemctl show gateway.service -p MainPID --value < "$PASS"; }

before=$(mainpid)
backups=()
for f in "$@"; do
  [ -n "$f" ] || continue
  case "$f" in
    src/gateway/*.py) ;;
    *) echo "只接受 src/gateway/*.py: $f" >&2; exit 2 ;;
  esac
  .venv/bin/python -c "import ast; ast.parse(open('$f', encoding='utf-8').read())" \
    || { echo "ast 预检失败: $f" >&2; exit 2; }
  b="$f.bak_$(date +%Y%m%d_%H%M%S)"
  cp "$f" "$b"
  backups+=("$b")
done

sudo -S -p "" systemctl restart gateway.service < "$PASS"
sleep 4

after=$(mainpid)
active=$(sudo -S -p "" systemctl is-active gateway.service < "$PASS")
[ "$active" = "active" ] || { echo "服务未 active: $active（sudo 静默失败？）" >&2; exit 1; }
[ "$before" != "$after" ] || { echo "MainPID 未变更（$before）：重启未生效" >&2; exit 1; }

curl -sf https://127.0.0.1:8080/health >/dev/null || { echo "/health 失败" >&2; exit 1; }

# 前端 chunk 核对（/admin/app 入口 no-cache 恒重验；-{8} 是 vite 哈希位别放宽）
entry=$(curl -sk https://127.0.0.1:8080/admin/app | grep -oE 'src="[^"]*\.js"' | head -1 | sed 's/src="//;s/"//')
missing=$(curl -sk "https://127.0.0.1:8080$entry" | grep -oE '[A-Za-z0-9]+-[A-Za-z0-9_-]{8}\.js' | sort -u \
  | while read -r c; do [ -f "static/admin/assets/$c" ] || echo "$c"; done)
[ -z "$missing" ] || { echo "缺产物: $missing" >&2; exit 1; }

for b in "${backups[@]}"; do rm -f "$b"; done
echo "部署 OK: MainPID $before → $after，health OK，chunk 齐，备份已清"
