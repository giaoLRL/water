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


# ---------- 可视化水位 ----------
def tank_capacity(tank: str = "storage") -> float:
    """指定水槽的容积(L)：把液位百分比换算成"估算水量"的分母。"""
    default = (config.TANK_CAPACITY_STORAGE_DEFAULT if tank == "storage"
               else config.TANK_CAPACITY_HEATER_DEFAULT)
    return get_float(f"tank_capacity_{tank}", default)


def target_baseline() -> float | None:
    """设定定量目标那一刻的累计水量(L)，用于计算本次定量已注入量；未记录返回 None。"""
    raw = get("target_baseline", None)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def set_target_baseline(value: float | None) -> None:
    """记录/清除定量基准；传 None 表示清除(取消定量或设备已清零)。"""
    set("target_baseline", "" if value is None else value)