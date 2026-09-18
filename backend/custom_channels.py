"""自定义传感器通道管理器（全链路，卡片即声明）。

实时监控页的「自定义接口卡」本身就是声明：卡片 source.kind == "custom" 时携带
url / path / period（不再有独立的通道声明表，系统配置页也不再重复填一遍）。
本管理器直接从仪表盘布局派生通道，在采集循环中按各通道周期轮询 JSON 取值：
  值有效 → 写入 realtime 快照（卡片/联动用）并入库 custom_sensor_data 表（历史/统计用）；
  无数据（取不到/接口失败）→ 跳过，不入库不做模拟。
通道 id 使用卡片 id，历史曲线 / 统计 / 联动均按它索引。
"""
import json
import threading
import time
import urllib.request
from urllib.parse import urlparse

import config
import database
import store


def load_channels() -> list[dict]:
    """从仪表盘布局派生自定义通道（卡片即声明）；过滤缺字段的无效卡片。"""
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
        if src.get("kind") != "custom":
            continue
        cid = str(w.get("id") or "").strip()
        url = str(src.get("url") or "").strip()
        path = str(src.get("path") or "").strip()
        if not cid or not url or not path:
            continue
        try:
            period = float(src.get("period") or 5)
        except (TypeError, ValueError):
            period = 5.0
        out.append({"id": cid, "name": str(w.get("title") or cid),
                    "unit": str(w.get("unit") or ""), "url": url, "path": path,
                    "period": period})
    return out


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

    def poll(self) -> None:
        """采集循环每周期调用：按通道 period 节流轮询并入库（无数据不入库）。"""
        now = time.time()
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
            value = _fetch_value(ch["url"], ch["path"])
            if value is None:
                continue
            with self._lock:
                self._values[ch["id"]] = value
            try:
                database.insert_custom_sensor(ch["id"], value)
            except Exception:  # noqa: BLE001
                pass  # 入库失败不影响采集循环
