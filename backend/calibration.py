"""通道标定：现场换传感器后把原始读数换算成工程值（原始值 × scale + offset）。

配置存 config 表 sys.channel_cal（JSON 对象）：
  {"flow_rate": {"scale": 0.1, "offset": 0, "unit": "L/min", "note": "脉冲计 K 值"},
   "custom:<卡片id>": {...}}
未配置的通道不做任何换算（scale=1、offset=0），保证既有数据不受影响。

应用点：
  - water_device：本机通道（流量/累计/双温度/压力/光照/液位）读取后立即换算，
    因此实时快照、入库、告警、历史、统计拿到的都是工程值；
  - custom_channels / gateway：自定义通道读数换算（key 形如 custom:<id>）。
标定页提供"两点标定"：给出两个「原始值 → 工程值」，自动求出 scale 与 offset。
"""
import threading

import store

_lock = threading.Lock()
_cache: dict[str, dict] = {}
_loaded = False

# 可标定的本机通道（现场按需换算，未配置即原值）
CHANNELS = ("flow_rate", "total_liters", "storage_temp", "heater_temp",
            "pressure", "light", "level_heater", "level_storage")


def load() -> dict:
    """读取标定配置（带进程内缓存，保存后调用 reload 生效）。"""
    global _cache, _loaded
    with _lock:
        if not _loaded:
            try:
                raw = store.get_json("channel_cal", {})
            except Exception:  # noqa: BLE001
                raw = {}
            _cache = raw if isinstance(raw, dict) else {}
            _loaded = True
        return dict(_cache)


def reload() -> None:
    """清缓存，下次读取重新从数据库加载。"""
    global _loaded
    with _lock:
        _loaded = False


def params(key: str) -> tuple[float, float]:
    """取某通道的 (scale, offset)，未配置返回 (1.0, 0.0)。"""
    item = load().get(key) or {}
    try:
        scale = float(item.get("scale") if item.get("scale") is not None else 1)
    except (TypeError, ValueError):
        scale = 1.0
    try:
        offset = float(item.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0.0
    return scale, offset


def apply(key: str, value):
    """换算单个读数：None 原样返回；未配置标定时原样返回（零开销路径）。"""
    if value is None:
        return None
    scale, offset = params(key)
    if scale == 1.0 and offset == 0.0:
        return value
    try:
        return round(float(value) * scale + offset, 4)
    except (TypeError, ValueError):
        return value


def two_point(raw1: float, eng1: float, raw2: float, eng2: float) -> dict:
    """两点标定：由两对「原始值 → 工程值」求 scale/offset（原始值相同则报错）。"""
    if abs(float(raw2) - float(raw1)) < 1e-9:
        raise ValueError("两个原始值不能相同")
    scale = (float(eng2) - float(eng1)) / (float(raw2) - float(raw1))
    offset = float(eng1) - scale * float(raw1)
    return {"scale": round(scale, 6), "offset": round(offset, 6)}
