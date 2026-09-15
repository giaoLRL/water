"""ESP32 采集/控制端 HTTP 客户端（仅用标准库 urllib，无第三方依赖，可离线）。

现场设备（默认 http://192.168.31.100）固件为《YF-S401 水流量检测》，实际提供的接口：
    GET /                          网页控制页(HTML)
    GET /api/data                  全部状态 JSON(瞬时流量/累计水量/水泵/定量目标/窗口脉冲)
    GET /api/flow                  瞬时流量 {"value":x,"unit":"L/min"}
    GET /api/volume                累计水量 {"value":x,"unit":"L"}
    GET /api/pump/on               开水泵
    GET /api/pump/off              关水泵
    GET /api/pump/toggle           切换水泵
    GET /api/pump/state            查询水泵 {"pump":true/false}
    GET /api/pump/target           查询定量目标 {"target":0}
    GET /api/pump/target?value=2.5 设定定量目标(水量达到自动关泵)
    GET /api/reset                 清零累计水量
    GET /api/health                运行时长/IP/RSSI/状态
    GET /api/temperature           水温；读数无效时 503，固件无该通道时 404

设计要点：
- 所有请求均为 GET（与现场固件一致），网络/HTTP/解析异常统一抛出 DeviceError；
- 由上层（water_device）决定离线降级策略，本模块不做任何模拟数据；
- 温度等可选通道返回 None 表示“不可用/读数无效”，不抛异常，避免影响主数据链路。
"""
import json
import urllib.error
import urllib.parse
import urllib.request


class DeviceError(RuntimeError):
    """设备通信异常：网络不可达、超时、HTTP 错误码或返回内容非法。"""


class Esp32Client:
    """ESP32 采集端 HTTP 客户端（无状态，线程安全）。"""

    def __init__(self, base_url: str, timeout: float = 1.5):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout

    # ---------- 底层请求 ----------
    def _url(self, path: str, params: dict | None = None) -> str:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _get(self, path: str, params: dict | None = None,
             tolerate: tuple[int, ...] = ()) -> tuple[int, dict | None]:
        """GET 并解析 JSON，返回 (状态码, 数据)。

        tolerate 中的 HTTP 状态码不视为错误（如温度的 503/404），返回 (码, None)。
        """
        if not self.base_url:
            raise DeviceError("未配置设备地址(WATER_DEVICE_URL)")
        url = self._url(path, params)
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:      # 注意：HTTPError 是 URLError 子类，须先捕获
            if exc.code in tolerate:
                return exc.code, None
            raise DeviceError(f"设备返回 HTTP {exc.code}（{path}）") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DeviceError(f"无法连接设备 {self.base_url}：{exc}") from exc

        if not raw.strip():
            return status, {}
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise DeviceError(f"设备返回非 JSON 数据（{path}）") from exc
        if not isinstance(payload, dict):
            raise DeviceError(f"设备返回格式非对象（{path}）")
        return status, payload

    @staticmethod
    def _as_float(value) -> float | None:
        try:
            return round(float(value), 3)
        except (TypeError, ValueError):
            return None

    # ---------- 采集 ----------
    def data(self) -> dict:
        """读取 /api/data 全部状态。"""
        _, payload = self._get("/api/data")
        return payload or {}

    def flow(self) -> dict:
        """读取 /api/flow，返回 {"value":x,"unit":"L/min"}。"""
        _, payload = self._get("/api/flow")
        return payload or {}

    def volume(self) -> dict:
        """读取 /api/volume，返回 {"value":x,"unit":"L"}。"""
        _, payload = self._get("/api/volume")
        return payload or {}

    def temperature(self) -> float | None:
        """读取 /api/temperature。

        503=读数无效（传感器异常），404=固件无该通道；两种情况均返回 None，
        由前端显示“读数无效 / 设备不支持”，不影响其他数据采集。
        """
        status, payload = self._get("/api/temperature", tolerate=(404, 503))
        if status != 200 or not payload:
            return None
        return self._as_float(payload.get("value"))

    def health(self) -> dict:
        """读取 /api/health（运行时长/IP/RSSI 等）。"""
        _, payload = self._get("/api/health")
        return payload or {}

    def level(self, path: str) -> dict | None:
        """读取液位（传感器尚未接入，接口路径由调用方传入 config.LEVEL_PATH，待固件确定）。

        兼容 {"value":x,"unit":"%"} 与 {"value":x,"unit":"cm"} 两种返回，
        单位换算由上层负责（cm 需用 config.TANK_HEIGHT_CM 换算为百分比）。
        返回 {"value":float|None, "unit":str|None}；通道不存在或读数无效时返回 None。
        """
        status, payload = self._get(path, tolerate=(404, 503))
        if status != 200 or not payload:
            return None
        unit = payload.get("unit")
        return {"value": self._as_float(payload.get("value")),
                "unit": (str(unit).strip().lower() or None) if unit else None}

    # ---------- 水泵控制 ----------
    def pump_state(self) -> bool | None:
        """查询水泵真实状态；设备未返回布尔值时返回 None。"""
        _, payload = self._get("/api/pump/state")
        value = (payload or {}).get("pump")
        return value if isinstance(value, bool) else None

    def pump_on(self) -> dict:
        return self._action("/api/pump/on", "pump")

    def pump_off(self) -> dict:
        return self._action("/api/pump/off", "pump")

    def pump_toggle(self) -> dict:
        return self._action("/api/pump/toggle", "pump")

    def _action(self, path: str, key: str) -> dict:
        """执行一个控制指令并校验 {"status":"ok"}。"""
        _, payload = self._get(path)
        payload = payload or {}
        if payload.get("status") not in (None, "ok"):
            raise DeviceError(f"设备执行失败（{path}）：{payload}")
        return payload

    # ---------- 定量目标 ----------
    def get_pump_target(self) -> float:
        """查询定量浇水目标(L)。"""
        _, payload = self._get("/api/pump/target")
        return self._as_float((payload or {}).get("target")) or 0.0

    def set_pump_target(self, liters: float) -> float:
        """设定定量目标(L)；累计水量达到该目标后设备自动关泵。返回设备回读值。"""
        _, payload = self._get("/api/pump/target", {"value": f"{float(liters):.2f}"})
        payload = payload or {}
        if payload.get("status") not in (None, "ok"):
            raise DeviceError(f"设定定量目标失败：{payload}")
        target = self._as_float(payload.get("target"))
        return target if target is not None else float(liters)

    # ---------- 累计水量清零 ----------
    def reset_volume(self) -> float:
        """清零累计水量，返回清零后的值(通常为 0)。"""
        _, payload = self._get("/api/reset")
        payload = payload or {}
        if payload.get("status") not in (None, "ok"):
            raise DeviceError(f"清零失败：{payload}")
        return self._as_float(payload.get("totalLiters")) or 0.0
