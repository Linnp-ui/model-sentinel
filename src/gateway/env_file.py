"""env 文件读改写（systemd EnvironmentFile 格式）——admin 配置 provider key 用。

两层生效：
  1. os.environ 运行时注入 —— Provider.api_key 每次调用读 os.getenv，**即时生效不用重启**
  2. 持久化 .env —— 行级替换/追加，重启（EnvironmentFile 重读）后保留

纪律（与 provider_keys 模块 docstring 一致）：
  - 密钥明文永不返回任何端点、永不进日志/审计（ops-log 只记变量名+长度）
  - 行级编辑：其余行逐字节保留；值含 空格/#/引号/反斜杠 时加双引号
    （EnvironmentFile 不支持行尾内联注释，整行等号后都是值）
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_QUOTE_RE = re.compile(r"""[\s"'#\\]""")


def env_file_path() -> Path:
    return Path(os.getenv("AI_GATEWAY_ENV_PATH") or (Path.cwd() / ".env"))


def _atomic_write(p: Path, lines: list) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:  # 保住原文件权限（os.replace 会带 tmp 的默认权限上来，丢组写位）
        os.chmod(tmp, p.stat().st_mode & 0o7777)
    except OSError:
        pass
    os.replace(tmp, p)


def set_env_value(name: str, value: str) -> dict:
    """设 env：os.environ 即时生效 + 落 .env（替换同名行，缺则追加）。返回 {env, length}。"""
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"env 变量名不合法：{name!r}（大写字母/数字/下划线）")
    value = (value or "").strip()
    if len(value) < 8:
        raise ValueError("key 太短（至少 8 位）")
    if "\n" in value or "\r" in value:
        raise ValueError("key 不能含换行")
    os.environ[name] = value
    p = env_file_path()
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    quoted = f'"{value}"' if _QUOTE_RE.search(value) else value
    out, replaced = [], False
    for ln in lines:
        if ln.strip().startswith(name + "="):
            out.append(f"{name}={quoted}")
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append(f"{name}={quoted}")
    _atomic_write(p, out)
    return {"env": name, "length": len(value)}


def clear_env_value(name: str) -> dict:
    """清 env：os.environ 移除 + .env 删行。"""
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"env 变量名不合法：{name!r}")
    os.environ.pop(name, None)
    p = env_file_path()
    if p.exists():
        lines = p.read_text(encoding="utf-8").splitlines()
        out = [ln for ln in lines if not ln.strip().startswith(name + "=")]
        if len(out) != len(lines):
            _atomic_write(p, out)
    return {"env": name}


def env_value_set(name: str) -> bool:
    return bool(os.getenv(name, "").strip())
