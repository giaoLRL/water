"""传感器通道管理器（全链路，卡片即声明）。

实时监控页的卡片本身就是声明，本管理器从仪表盘布局派生通道：
  1) **自定义接口卡**（source.kind == "custom"）：携带 url / path / period，
     后端按周期 GET 该接口取值（主机须在代理白名单）；
  2) **本机快照卡**（source.kind == "realtime"）：数值卡/曲线卡/单水槽卡读的是本机快照，
     其中**不属于 water_sensors 标准列**的路径（如 device.rssi、tank.tanks.*、pulses）
     也在采集循环里按周期取样入库，通道 id 为 "card:<卡片id>"。
     —— 这样"在实时监控页加一张卡"就等于"历史/统计多一个指标"；
     标准列（流量/累计/双温/压力/光照/泵/加热状态/定量目标）本来就在 water_sensors 里，不重复存。
值有效 → 写入 realtime 快照（卡片/联动用）并入库 custom_sensor_data 表（历史/统计用）；
无数据（取不到/接口失败/快照无此字段）→ 跳过，不入库不做模拟。
"""
import json
import threading
import time
import urllib.request
from urllib.parse import urlparse

import config
import calibration
import database
import store

# 已在 water_sensors 表里逐秒入库的快照路径：这些不需要再重复存一份
STORED_REALTIME_PATHS = {
    "flow_rate", "total_liters", "total_flow", "storage_temp", "heater_temp",
    "pressure", "light", "pump_state", "heater_state", "pump_target",
}
# 参与派生的卡片类型（只取数值类；控制卡/内置操作卡不产生新指标）
REALTIME_CARD_TYPES = ("value", "spark", "line", "tank")


def load_channels() -> list[dict]:
    """从仪表盘布局派生通道（卡片即声明）；过滤缺字段的无效卡片。"""
    try:
        layout = store.get_json("dashboard_layout", None)
    except Exception:  # noqa: BLE001
        layout = None
    widgets = (layout or {}).get("widgets") if isinstance(layout, dict) else None
    out: list[dict] = []
    for w in widgets or []:
        if not isinstance(w, dict):
            continue
        src = w.get("source") or {}
        kind = src.get("kind")
        cid = str(w.get("id") or "").strip()
        if not cid:
            continue
        path = str(src.get("path") or "").strip()
        if not path:
            continue
        try:
            period = float(src.get("period") or 5)
        except (TypeError, ValueError):
            period = 5.0
        if kind == "custom":
            url = str(src.get("url") or "").strip()
            if not url:
                continue
            out.append({"id": cid, "name": str(w.get("title") or cid),
                        "unit": str(w.get("unit") or ""), "url": url, "path": path,
                        "period": period, "kind": "custom"})
        elif kind == "realtime" and str(w.get("type")) in REALTIME_CARD_TYPES:
            if path in STORED_REALTIME_PATHS:
                continue          # 该字段已在 water_sensors 逐秒入库，避免重复采集
            out.append({"id": "card:" + cid, "name": str(w.get("title") or cid),
                        "unit": str(w.get("unit") or ""), "url": "", "path": path,
                        "period": period, "kind": "realtime"})
    return out


def _get_path(obj, path: str):
    """按 a.b.c 路径取值，取不到返回 None。"""
    try:
        for k in str(path).split("."):
            obj = obj[k] if isinstance(obj, dict) else None
        return obj
    except (KeyError, TypeError):
        return None


def _numeric(value):
    """读数归一化为 float：水槽卡等对象源取 percent；非数值返回 None。"""
    if isinstance(value, dict):
        value = value.get("percent")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_value(url: str, path: str):
    """拉取单通道读数：白名单校验 → GET → JSON 按 a.b.c 路径取值 → float；失败返回 None。"""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    if u.hostname not in config.DASHBOARD_PROXY_HOSTS:
        return None
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=config.DEVICE_TIMEOUT_S) as resp:
            payload = json.loads(resp.read(131_072).decode("utf-8", "replace"))
        obj = payload
        for k in str(path).split("."):
            obj = obj[k] if isinstance(obj, dict) else None
        if obj is None:
            return None
        return float(obj)
    except Exception:  # noqa: BLE001
        return None


class CustomChannelManager:
    """按通道周期轮询自定义传感器，维护实时值并入库。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._channels: list[dict] = []
        self._ts: dict[str, float] = {}     # 通道id -> 上次轮询时间
        self._values: dict[str, float] = {} # 通道id -> 最近一次有效读数
        self.reload()

    def reload(self) -> None:
        """重新加载通道声明（保存后调用，即时生效）。"""
        with self._lock:
            self._channels = load_channels()
            ids = {c["id"] for c in self._channels}
            self._values = {k: v for k, v in self._values.items() if k in ids}

    def channels(self) -> list[dict]:
        with self._lock:
            return list(self._channels)

    def values(self) -> dict:
        """最近一次有效读数 {通道id: value}（供实时快照与联动）。"""
        with self._lock:
            return dict(self._values)

    def poll(self, data: dict | None = None) -> None:
        """采集循环每周期调用：按通道 period 节流取数并入库（无数据不入库）。

        data 为本轮采集快照（含本机通道与外部设备读数）：kind=realtime 的通道直接从中按路径取值；
        kind=custom 的通道仍走 HTTP 轮询。
        """
        now = time.time()
        snapshot = dict(data or {})
        snapshot.setdefault("custom", self.values())   # 允许卡片路径引用 custom.<通道id>
        with self._lock:
            tasks = [(c, self._ts.get(c["id"], 0.0)) for c in self._channels]
        for ch, last in tasks:
            try:
                period = max(1.0, float(ch.get("period") or 5))
            except (TypeError, ValueError):
                period = 5.0
            if now - last < period:
                continue
            with self._lock:
                self._ts[ch["id"]] = now   # 先占位：请求慢也不会每周期重复打
            if ch.get("kind") == "realtime":
                value = _numeric(_get_path(snapshot, ch["path"]))   # 本机快照取样（水槽卡取 percent）
            else:
                value = _fetch_value(ch["url"], ch["path"])
            if value is None:
                continue
            value = calibration.apply(str(ch["id"]), value)   # 通道级标定（未配置则原值）
            with self._lock:
                self._values[ch["id"]] = value
            try:
                database.insert_custom_sensor(ch["id"], value)
            except Exception:  # noqa: BLE001
                pass  # 入库失败不影响采集循环
