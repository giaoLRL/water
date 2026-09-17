"""水循环设备模型（纯真实模式）：通过 HTTP 读写现场 ESP32 采集端。

分层（业务层与底层解耦）：
    业务层(api / main / alarm) 只调用 read / pump_control / heater_control / set_pump_target / reset_volume；
    HTTP 细节封装在 esp32_client.Esp32Client。

现场固件 web_server.cpp 实际提供的通道：
    瞬时流量 flow_rate(L/min)、累计水量 total_liters(L)、水温×2(探头1→储水槽、探头2→加热槽)、
    压力(固件 MPa，统一换算 kPa)、水泵 pump、加热继电器 heater、定量目标 pump_target(L)、
    上一窗口脉冲数 pulses / 窗口时长 window_ms、设备健康(IP/RSSI/运行时长)。
    以上通道固件在 /api/data 一次全部带回，无需逐个请求。
    另有：超声波液位 /api/level(单路，接加热槽，返回百分比)、光照 /api/light(lx)，
    这两个通道每周期单独请求。

温度/压力/加热/光照通道由 config.DEVICE_FEATURES 声明开关；关闭时对应字段恒为 None，
前端据此显示"设备不支持"。读数无效（tempOk/pressureOk 为 False 或 503）时为 None，
系统不生成任何模拟值。设备离线时字段置 None 并置 sensor_online=False，由前端提示"设备离线"。

液位通道（双水槽）：仅加热槽接超声波传感器，未接入的槽位水位恒为 0；
设备离线后两槽水位统一归 0，同样不做任何模拟。
"""
import threading
import time

import config
from esp32_client import DeviceError, Esp32Client


def _clamp(percent: float) -> float:
    """把液位百分比限制在 0~100。"""
    return max(0.0, min(100.0, float(percent)))


class WaterPlant:
    """单套水循环设备：流量/水量/水温×2/压力/水泵/加热（按 DEVICE_FEATURES 可裁剪）。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.client = Esp32Client(config.DEVICE_URL, config.DEVICE_TIMEOUT_S)
        # 实时读数（None 表示无有效数据：设备离线或该通道不被固件支持）
        self.flow_rate: float | None = None
        self.total_liters: float | None = None
        self.pump_state: str = "unknown"      # on / off / unknown
        self.pump_target: float = 0.0
        self.pulses: int | None = None        # 上一窗口脉冲数
        self.window_ms: float | None = None   # 上一窗口时长(ms)，固件用 lastUpdateMs 返回
        # 温度/压力/加热通道（/api/data 一次带回；离线或读数无效时为 None）
        self.storage_temp: float | None = None   # 探头1 → 储水槽水温℃
        self.heater_temp: float | None = None    # 探头2 → 加热槽水温℃
        self.pressure: float | None = None       # 水压 kPa（固件 MPa × 1000）
        self.heater_state: str | None = None     # on / off / None(未知)
        # 液位通道（双水槽）：仅加热槽接超声波传感器；未接入的槽位恒为 0，不做任何模拟
        self.level_storage: float = 0.0
        self.level_heater: float = 0.0
        # 光照通道（/api/light，lx；GY-302 未识别时固件 503 → None）
        self.light: float | None = None
        # 链路状态
        self.sensor_online = False
        self.last_error = ""
        self.health: dict = {}                # /api/health 原始内容
        self._last_ok_ts = 0.0
        self._last_health_ts = 0.0

    # ---------- 采集 ----------
    def read(self) -> dict:
        """采集一次设备状态（≥1Hz 调用），返回标准化后的字段字典。

        网络失败不清空最近读数：仅在持续无响应超过 DEVICE_OFFLINE_AFTER_S 后判定离线并清空，
        避免 ESP32 偶发丢包导致界面数据闪烁。
        """
        with self._lock:
            try:
                payload = self.client.data()
            except DeviceError as exc:
                self._mark_failed(str(exc))
                return self.status()
            self._apply(payload)

            self._update_levels()

            # 光照：独立接口 /api/light（GY-302 未识别时 503 → None；网络异常保留上次读数）
            if config.DEVICE_FEATURES.get("light"):
                try:
                    self.light = self.client.light()
                except DeviceError:
                    pass

            # 设备健康信息（IP/RSSI/运行时长）按较低频率刷新，减少设备连接压力
            now = time.time()
            if now - self._last_health_ts >= config.DEVICE_HEALTH_PERIOD_S:
                self._last_health_ts = now
                try:
                    self.health = self.client.health()
                except DeviceError:
                    pass
            return self.status()

    def _update_levels(self) -> None:
        """更新双水槽液位：仅读取已接入传感器的槽位。

        未接入的通道（DEVICE_FEATURES 为 False / LEVEL_PATH 为空）恒为 0，
        不做任何模拟。调用方需已持有 self._lock。
        """
        for tank in config.TANKS:
            if config.DEVICE_FEATURES.get(f"level_{tank}"):
                self._read_level(tank)

    @staticmethod
    def _valid(value, ok_flag) -> float | None:
        """读数有效性判定：ok 位为 False 或数值非法时返回 None（不伪造数据）。

        ok_flag 缺失(None)时视为有效，仅按数值能否解析判断。
        """
        if ok_flag is False:
            return None
        try:
            return round(float(value), 3)
        except (TypeError, ValueError):
            return None

    def _apply(self, payload: dict) -> None:
        """把 /api/data 原始字段标准化到实例属性。

        温度/压力/加热通道按 DEVICE_FEATURES 裁剪；固件在 /api/data 一次带回全部字段，
        无需逐通道发请求（tempOk/tempOk2/pressureOk 为读数质量位）。
        """
        self.flow_rate = self.client._as_float(payload.get("flowRate"))
        self.total_liters = self.client._as_float(payload.get("totalLiters"))
        self.pump_target = self.client._as_float(payload.get("pumpTarget")) or 0.0
        self.pulses = payload.get("pulsesInLastWindow")
        self.window_ms = self.client._as_float(payload.get("lastUpdateMs"))
        pump = payload.get("pump")
        if isinstance(pump, bool):
            self.pump_state = "on" if pump else "off"
        # 温度×2：探头1→储水槽，探头2→加热槽
        if config.DEVICE_FEATURES.get("temperature"):
            self.storage_temp = self._valid(payload.get("temperature"), payload.get("tempOk"))
            self.heater_temp = self._valid(payload.get("temperature2"), payload.get("tempOk2"))
        # 压力：固件单位 MPa，统一换算为 kPa 入库与展示
        if config.DEVICE_FEATURES.get("pressure"):
            pressure_mpa = self._valid(payload.get("pressure"), payload.get("pressureOk"))
            self.pressure = round(pressure_mpa * 1000.0, 3) if pressure_mpa is not None else None
        # 加热继电器状态
        if config.DEVICE_FEATURES.get("heater"):
            heater = payload.get("heater")
            if isinstance(heater, bool):
                self.heater_state = "on" if heater else "off"
        # 流量读取成功即视为在线
        self.sensor_online = True
        self.last_error = ""
        self._last_ok_ts = time.time()

    def _mark_failed(self, message: str) -> None:
        """通信失败：先标记离线；持续超时才清空读数，避免界面闪烁。"""
        self.sensor_online = False
        self.last_error = message
        if time.time() - self._last_ok_ts > config.DEVICE_OFFLINE_AFTER_S:
            self.flow_rate = None
            self.total_liters = None
            self.pump_state = "unknown"
            self.pulses = None
            self.window_ms = None
            self.storage_temp = None
            self.heater_temp = None
            self.pressure = None
            self.heater_state = None
            self.light = None
            # 液位无数据一律归 0（不做模拟，也不保留旧值），界面同时提示设备离线
            self.level_storage = 0.0
            self.level_heater = 0.0

    def _read_level(self, tank: str) -> None:
        """读取指定水槽的液位传感器。支持 % 与 cm/mm 两种单位。

        无数据一律归 0（用户要求：清除模拟数据，没有数据就保持 0）：
          - 未配置传感器路径     → 0（该槽无数据来源，界面标注「无传感器」）
          - 通道返回 503/404     → 0（传感器异常或通道缺失，属实无有效读数）
          - 网络异常(DeviceError) → 保留上次读数，避免单次丢包造成水位闪烁；
                                   持续离线由 _mark_failed 归 0
        """
        path = (config.LEVEL_PATH_STORAGE if tank == "storage" else config.LEVEL_PATH_HEATER)
        if not path:
            setattr(self, f"level_{tank}", 0.0)
            return
        height = (config.TANK_HEIGHT_CM_STORAGE if tank == "storage"
                  else config.TANK_HEIGHT_CM_HEATER)
        try:
            payload = self.client.level(path)
        except DeviceError:
            return
        if not payload or payload.get("value") is None:
            setattr(self, f"level_{tank}", 0.0)
            return
        value = float(payload["value"])
        unit = payload.get("unit")
        if unit in ("cm", "mm"):
            # cm/mm 读数按该水槽高度换算成百分比
            if unit == "mm":
                value = value / 10.0
            if height > 0:
                value = value / height * 100.0
        setattr(self, f"level_{tank}", _clamp(value))

    def status(self) -> dict:
        """返回当前缓存状态的标准化字典（不发起网络请求）。"""
        with self._lock:
            return {
                "flow_rate": self.flow_rate,
                "total_liters": self.total_liters,
                "total_flow": self.total_liters,     # 兼容旧字段名：累计水量(L)
                "pump_state": self.pump_state,
                "pump_target": self.pump_target,
                "pulses": self.pulses,
                "window_ms": self.window_ms,
                # 液位通道（双水槽；仅加热槽为超声波实测，未接入或无数据时恒为 0）
                "level_storage": round(self.level_storage, 2),
                "level_heater": round(self.level_heater, 2),
                # 温度/压力/加热/光照通道（离线或读数无效时为 None）
                "temperature": self.storage_temp,   # 兼容旧字段名：探头1=储水槽水温
                "storage_temp": self.storage_temp,
                "heater_temp": self.heater_temp,
                "pressure": self.pressure,
                "heater_state": self.heater_state,
                "light": self.light,
                # 链路
                "sensor_online": self.sensor_online,
                "last_error": self.last_error,
                "device": self._device_info(),
                "features": dict(config.DEVICE_FEATURES),
            }

    def _device_info(self) -> dict:
        """设备健康信息（来自 /api/health，读不到时只给配置地址）。"""
        h = self.health or {}
        return {
            "url": config.DEVICE_URL,
            "status": h.get("status"),
            "ip": h.get("ip"),
            "rssi": h.get("rssi"),
            "uptime_ms": h.get("uptimeMs"),
        }

    # ---------- 执行器控制 ----------
    def pump_control(self, action: str) -> None:
        """水泵开关：action 为 on / off / toggle。

        指令下发后回读设备真实状态，保证界面显示与硬件一致；
        通信失败直接抛出 DeviceError，由接口层返回 40003（不伪造成功）。
        """
        if not config.DEVICE_FEATURES.get("pump"):
            raise DeviceError("当前采集设备不支持水泵控制")
        if action not in ("on", "off", "toggle"):
            raise DeviceError(f"不支持的水泵指令: {action}")
        with self._lock:
            if action == "on":
                payload = self.client.pump_on()
            elif action == "off":
                payload = self.client.pump_off()
            else:
                payload = self.client.pump_toggle()
            # 优先用指令回包，回包无布尔值时回读一次真实状态
            state = payload.get("pump")
            if not isinstance(state, bool):
                state = self.client.pump_state()
            if isinstance(state, bool):
                self.pump_state = "on" if state else "off"
            self.sensor_online = True
            self.last_error = ""
            self._last_ok_ts = time.time()

    def heater_control(self, action: str) -> None:
        """加热模块开关：action 为 on / off / toggle。

        指令下发后回读设备真实状态，保证界面显示与硬件一致；
        通信失败直接抛出 DeviceError，由接口层返回 40003（不伪造成功）。
        """
        if not config.DEVICE_FEATURES.get("heater"):
            raise DeviceError("当前采集设备不支持加热模块控制")
        if action not in ("on", "off", "toggle"):
            raise DeviceError(f"不支持的加热指令: {action}")
        with self._lock:
            if action == "on":
                payload = self.client.heater_on()
            elif action == "off":
                payload = self.client.heater_off()
            else:
                payload = self.client.heater_toggle()
            # 优先用指令回包，回包无布尔值时回读一次真实状态
            state = payload.get("heater")
            if not isinstance(state, bool):
                state = self.client.heater_state()
            if isinstance(state, bool):
                self.heater_state = "on" if state else "off"
            self.sensor_online = True
            self.last_error = ""
            self._last_ok_ts = time.time()

    # ---------- 定量浇水 ----------
    def set_pump_target(self, liters: float) -> float:
        """设定定量目标(L)：累计水量达到该值后由设备固件自动关泵。"""
        if not config.DEVICE_FEATURES.get("pump_target"):
            raise DeviceError("当前采集设备不支持定量目标")
        with self._lock:
            target = self.client.set_pump_target(liters)
            self.pump_target = target
            return target

    # ---------- 累计水量清零 ----------
    def reset_volume(self) -> float:
        """清零累计水量（破坏性操作，前端需二次确认）。"""
        if not config.DEVICE_FEATURES.get("volume_reset"):
            raise DeviceError("当前采集设备不支持累计水量清零")
        with self._lock:
            self.total_liters = self.client.reset_volume()
            return self.total_liters
