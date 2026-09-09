"""动态配置存储：把运行期可修改配置持久化到 MySQL config 表(sys.* 前缀)。

标量用 get/set，结构用 get_json/set_json；数据库为空时回退 config.py 默认值。
各模块每次读取即时生效，无需重启。
"""
import json

import config
import database

_PREFIX = "sys."


def get(key: str, default: str | None = None) -> str | None:
    return database.get_config(_PREFIX + key, default)


def set(key: str, value) -> None:
    database.set_config(_PREFIX + key, str(value))


def get_float(key: str, default: float) -> float:
    try:
        return float(get(key, str(default)))
    except (TypeError, ValueError):
        return default


def get_int(key: str, default: int) -> int:
    try:
        return int(float(get(key, str(default))))
    except (TypeError, ValueError):
        return default


def get_json(key: str, default):
    raw = get(key, None)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def set_json(key: str, value) -> None:
    database.set_config(_PREFIX + key, json.dumps(value, ensure_ascii=False))


# ---------- 领域运行期配置 ----------
def period() -> float:
    """采集周期(秒)，前端系统配置可调，默认 1.0。"""
    return get_float("period", config.SAMPLE_PERIOD_S)


def target_temp() -> float:
    return get_float("target_temp", config.TARGET_TEMP_DEFAULT)


def pid_kp() -> float:
    return get_float("pid_kp", config.PID_KP)


def pid_ki() -> float:
    return get_float("pid_ki", config.PID_KI)


def pid_kd() -> float:
    return get_float("pid_kd", config.PID_KD)


def pid_enabled() -> int:
    return get_int("pid_enabled", config.PID_ENABLED_DEFAULT)


def judge_enabled() -> int:
    return get_int("judge_enabled", config.JUDGE_ENABLED_DEFAULT)