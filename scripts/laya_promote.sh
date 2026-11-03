#!/usr/bin/env bash
# L2 影子 promote：预检 → 备份指针 → 写指针 → 重启 → /health 核对 → 失败自动回滚
# 用法：scripts/laya_promote.sh <版本名|目录>     例：scripts/laya_promote.sh v34
#   版本名 = /home/your-user/models/laya-finetuned-confidential-<版本名>
# 预检拦的是「训练未跑完的中途快照」（只有 checkpoint_latest/，缺 rl_agent_config.json）——
# 2026-11-03 v35 事故就是绕过这一步直接把指针切到残缺权重，崩溃循环了 15.5h。
# 只切影子，主链路不受影响；切换窗口内影子打标短暂缺失属预期。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

PASS=~/.config/opencode/.sudo_pass
ENV=var/laya-l2.env
HEALTH=http://127.0.0.1:8003/health
MODELS=/home/your-user/models/laya-finetuned-confidential-

[ $# -eq 1 ] || { echo "用法: $0 <版本名|目录>" >&2; exit 2; }
target=$1
case "$target" in
  /*) ;;
  *) target="$MODELS$target" ;;
esac
[ -f "$ENV" ] || { echo "缺 $ENV（现役指针的唯一真源）" >&2; exit 2; }

# 预检：完整权重 = 扁平文件 + 配置（训练中途只有 checkpoint_latest/，缺 rl_agent_config.json）
for f in rl_agent_config.json model.safetensors encoder tokenizer; do
  [ -e "$target/$f" ] || { echo "预检失败：$target 缺 $f（训练未完成/未打包的中途快照？）" >&2; exit 2; }
done

wait_laya() {  # $1 = 期望的 model 名（/health 返回的路径基名）；最多等 150s
  local body
  for _ in $(seq 1 75); do
    body=$(curl -sf -m 2 "$HEALTH" || true)
    case "$body" in *'"ok":true'*"\"model\":\"$1\""*) return 0;; esac
    sleep 2
  done
  return 1
}

old=$(sed -n 's/^LAYA_MODEL_PATH=//p' "$ENV")
expected=$(basename "$target")
backup="$ENV.bak_$(date +%Y%m%d_%H%M%S)"
cp "$ENV" "$backup"
printf 'LAYA_MODEL_PATH=%s\n' "$target" > "$ENV.tmp" && mv "$ENV.tmp" "$ENV"

sudo -S -p "" systemctl restart laya-l2.service < "$PASS"
if wait_laya "$expected"; then
  rm -f "$backup"
  echo "promote OK: laya → $expected（health 核对通过，备份已清）"
  exit 0
fi

echo "promote 失败：$expected 未通过 /health，回滚到 $(basename "${old:-?}")" >&2
printf 'LAYA_MODEL_PATH=%s\n' "$old" > "$ENV.tmp" && mv "$ENV.tmp" "$ENV"
sudo -S -p "" systemctl restart laya-l2.service < "$PASS"
wait_laya "$(basename "$old")" || { echo "回滚后仍不健康：需人工介入（指针备份 $backup）" >&2; exit 2; }
echo "已回滚并复核健康：$(basename "$old")（指针备份保留 $backup）" >&2
exit 1
