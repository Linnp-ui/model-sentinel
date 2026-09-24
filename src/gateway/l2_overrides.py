"""L2 运行配置持久化（T46，2026-09-17）。

PUT /admin/api/l2-config 的内存覆盖原本重启即丢（ephemeral）。本模块把白名单键
原子写入 l2_overrides.yaml（仓库根，AI_GATEWAY_L2_OVERRIDES_PATH 可覆盖），
进程启动时应用回 os.environ => 保存后重启仍生效。

纪律：
- 键必须是 L2ConfigPutReq 字段白名单（ALLOWED_KEYS），禁止任意 env 注入；
- 写盘走 tempfile + os.replace 原子替换；
- 读盘任何异常只 print 一行并返回 {}（不阻断启动），与仓库「静默降级必须留痕」一致。
"""
import os
import tempfile
from pathlib import Path

import yaml

DEFAULT_OVERRIDES_PATH = Path(__file__).parent.parent.parent / "l2_overrides.yaml"

# 与 admin_api.L2ConfigPutReq 字段一致（唯一真相在 admin_api，这里是二次防线）
ALLOWED_KEYS = frozenset({
    "SMALL_MODEL_ENABLED",
    "SMALL_MODEL_TIMEOUT",
    "SMALL_MODEL_URL",
    "SMALL_MODEL_NAME",
    "SMALL_MODEL_MAX_TOKENS",
    "AI_GATEWAY_L2_BACKEND",
    "AI_GATEWAY_L2_SHADOW",
    "AI_GATEWAY_L2_SHADOW_URL",
    "AI_GATEWAY_WL_L2_SAMPLE",
    "AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD",
    "AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD",
    "AI_GATEWAY_SUGGEST_ERROR_RATE",
    # L2 提示词：system prompt + user 模板（{filename}/{sheet_names}/{headers}/{preview} 占位符）
    "AI_GATEWAY_L2_SYSTEM_PROMPT",
    "AI_GATEWAY_L2_USER_PROMPT_TEMPLATE",
})


def overrides_path() -> Path:
    return Path(os.getenv("AI_GATEWAY_L2_OVERRIDES_PATH", "") or DEFAULT_OVERRIDES_PATH)


def to_env_str(v) -> str:
    """与 PUT 的 env 归一化保持一致：bool -> 'true'/'false'，其余 str()。"""
    return str(v).lower() if isinstance(v, bool) else str(v)


def load_overrides() -> dict:
    """读持久化覆盖；文件缺失返回 {}，损坏打印一行并忽略（不阻断启动）。"""
    p = overrides_path()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001 - 任何损坏都不阻断启动，但必须留痕
        print(f"[l2_overrides] 读取失败，忽略持久化覆盖 path={p} err={e!r}")
        return {}
    if not isinstance(data, dict):
        print(f"[l2_overrides] 文件顶层不是映射，忽略 path={p}")
        return {}
    return {k: v for k, v in data.items() if k in ALLOWED_KEYS and v is not None}


def apply_overrides() -> dict:
    """启动/显式调用时把持久化覆盖写回 os.environ。返回实际应用的映射。"""
    applied = {}
    for k, v in load_overrides().items():
        os.environ[k] = to_env_str(v)
        applied[k] = v
    if applied:
        print(f"[l2_overrides] 已应用 {len(applied)} 项持久化覆盖: {sorted(applied)}")
    return applied


def save_overrides(values: dict, *, delete: set | None = None) -> dict:
    """合并写盘：新值覆盖同名键；delete 集合里的键从最终结果移除。原子替换。

    返回落盘的映射。无任何变更时仍重写一次（保持调用方语义一致）。
    """
    merged = load_overrides()
    for k in values.items() if values else []:
        if k[0] in ALLOWED_KEYS and k[1] is not None:
            merged[k[0]] = k[1]
    for k in (delete or ()):
        if k in ALLOWED_KEYS:
            merged.pop(k, None)
    p = overrides_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".l2_overrides.", suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=True)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return merged
