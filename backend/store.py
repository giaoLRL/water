"""动态配置存储：把能够在"系统配置"页面修改的配置持久化到 MySQL config 表。

设计：
- 所有可动态配置项以 sys.* 前缀保存在 config 表（config_key/value）；
- 标量（服务地址、超时、间隔）用 get/set；结构（灯杆列表、传感器字段映射）用 get_json/set_json；
- 各模块通过本模块读取配置：要么每次操作时读（改完即时生效），要么按自身节奏重读；
- 数据库为空时回退到 config.py 里的代码默认值，保证"零配置也能跑"。
"""
import json

import config
import database

_PREFIX = "sys."


# ---------- 标量 ----------
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


# ---------- 结构（JSON） ----------
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


# ---------- 领域便捷读取 ----------
def lamp_posts() -> list[dict]:
    """当前灯杆列表（前端可增删改），回退到代码默认。"""
    return [dict(item) for item in get_json("lamp_posts", config.LAMP_POSTS)]


def sensor_fields() -> dict:
    """传感器返回字段映射（前端可编辑，适配不同品牌的 ESP32/传感器）。"""
    return dict(get_json("sensor_fields", {
        "status": "status",
        "temperature": "temperature",
        "humidity": "humidity",
        "light": "light",
        "smoke": "smokeRaw",
        "smoke_alarm": "smokeAlarm",
    }))


def lamp_ctrl_default() -> dict:
    """灯控接口全局默认格式（fallback，逐灯杆未配置灯控接口时使用）。"""
    return dict(get_json("lamp_ctrl_default", {
        "on": "/api/lamp/on",
        "off": "/api/lamp/off",
        "state": "/api/lamp/state",
        "field": "lamp",
        "status": "status",
    }))


def infer_url() -> str:
    return get("infer_url", config.INFER_URL)


def infer_timeout() -> float:
    return get_float("infer_timeout", config.INFER_TIMEOUT)


def person_detect_interval() -> float:
    return get_float("person_detect_interval", config.PERSON_DETECT_INTERVAL)


def sample_interval() -> float:
    return get_float("sample_interval", config.SAMPLE_INTERVAL)


def monitor_interval() -> float:
    """设备在线状态探测间隔（秒）。"""
    return get_float("monitor_interval", 10.0)


def monitor_timeout() -> float:
    """设备在线状态单次探测超时（秒）。"""
    return get_float("monitor_timeout", 1.5)