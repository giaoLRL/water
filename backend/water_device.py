"""水循环设备模型（纯真实模式）：通过 HTTP 读写现场 ESP32 采集端。

分层（业务层与底层解耦）：
    业务层(api / main / alarm) 只调用 read / pump_control / set_pump_target / reset_volume；
    HTTP 细节封装在 esp32_client.Esp32Client。

现场固件《YF-S401 水流量检测》实际提供的通道：
    瞬时流量 flow_rate(L/min)、累计水量 total_liters(L)、水泵 pump、定量目标 pump_target(L)、
    上一窗口脉冲数 pulses / 窗口时长 window_ms、设备健康(IP/RSSI/运行时长)。

温度、压力、加热通道由 config.DEVICE_FEATURES 声明；当前固件不支持，对应字段恒为 None，
前端据此显示“设备不支持”，系统不会生成任何模拟值。
设备离线时字段置 None 并置 sensor_online=False，由前端提示“设备离线”。
"""
import threading
import time

import config
from esp32_client import DeviceError, Esp32Client


def _clamp(percent: float) -> float:
    """把液位百分比限制在 0~100。"""
    return max(0.0, min(100.0, float(percent)))


class WaterPlant:
    """单套水循环设备：1 路流量 + 累计水量 + 水泵控制（按 DEVICE_FEATURES 可扩展）。"""

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
        # 设备未提供的通道，恒为 None（不模拟）
        self.temperature: float | None = None
        self.pressure: float | None = None
        self.heater_state: str | None = None
        # 液位通道（双水槽）：传感器尚未接入，先用模拟值（界面会标注"模拟"）
        self.level_storage: float = _clamp(config.SIM_LEVEL_STORAGE_INITIAL)
        self.level_heater: float = _clamp(config.SIM_LEVEL_HEATER_INITIAL)
        self._last_level_ts = time.time()
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
                # 模拟水位是本地生成的，不应因设备掉线而冻结，否则界面动画会"卡死"
                self._update_levels()
                return self.status()
            self._apply(payload)

            # 温度通道：仅当固件声明支持时读取（当前固件不支持，恒为 None）
            if config.DEVICE_FEATURES.get("temperature"):
                try:
                    self.temperature = self.client.temperature()
                except DeviceError:
                    self.temperature = None

            self._update_levels()

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
        """更新双水槽液位：已接入的读真实传感器，未接入的走模拟。

        模拟部分与设备通信解耦——设备掉线时水位仍按"无水流"继续演化，
        避免动画冻结。调用方需已持有 self._lock。
        """
        need_sim = False
        for tank in config.TANKS:
            if config.DEVICE_FEATURES.get(f"level_{tank}"):
                self._read_level(tank)
            else:
                need_sim = True
        if need_sim:
            self._simulate_levels()

    def _apply(self, payload: dict) -> None:
        """把 /api/data 原始字段标准化到实例属性。"""
        self.flow_rate = self.client._as_float(payload.get("flowRate"))
        self.total_liters = self.client._as_float(payload.get("totalLiters"))
        self.pump_target = self.client._as_float(payload.get("pumpTarget")) or 0.0
        self.pulses = payload.get("pulsesInLastWindow")
        self.window_ms = self.client._as_float(payload.get("lastUpdateMs"))
        pump = payload.get("pump")
        if isinstance(pump, bool):
            self.pump_state = "on" if pump else "off"
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
            self.temperature = None
            # 液位为本地模拟值，与设备通信无关，离线时保留不置空

    def _read_level(self, tank: str) -> None:
        """读取指定水槽的真实液位传感器（传感器接入后启用）。支持 % 与 cm/mm 两种单位。"""
        path = (config.LEVEL_PATH_STORAGE if tank == "storage" else config.LEVEL_PATH_HEATER)
        height = (config.TANK_HEIGHT_CM_STORAGE if tank == "storage"
                  else config.TANK_HEIGHT_CM_HEATER)
        try:
            payload = self.client.level(path)
        except DeviceError:
            payload = None
        if not payload or payload.get("value") is None:
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

    def _simulate_levels(self) -> None:
        """模拟双水槽液位（传感器未接入时使用，仅演示用，非真实液位）。

        按「水量守恒」模拟一套闭合水循环：
          水泵运行(有流量) → 把水从储水槽送到加热槽，储水槽保留 SIM_LEVEL_STORAGE_MIN
                             作为最低水位（防抽干），两槽都不越界；
          水泵停止         → 两槽通过回路缓慢回平（水位差按比例收敛）。
        dt 单步上限 2s，防止长时间暂停后一步跳变。
        只对「未接入真实传感器」的水槽生效；已接入的一侧保持实测值，并参与守恒计算。
        """
        now = time.time()
        dt = min(now - self._last_level_ts, 2.0)
        self._last_level_ts = now

        sim_storage = not config.DEVICE_FEATURES.get("level_storage")
        sim_heater = not config.DEVICE_FEATURES.get("level_heater")
        if not (sim_storage or sim_heater) or dt <= 0:
            return

        storage, heater = self.level_storage, self.level_heater
        flow = float(self.flow_rate or 0.0)

        if flow > 0.1:
            ref = config.SIM_LEVEL_FLOW_REF
            speed = min(2.0, flow / ref) if ref > 0 else 1.0
            move = config.SIM_LEVEL_TRANSFER_PER_S * speed * dt
            # 受限于：储水槽不抽干、加热槽不满溢
            if sim_storage:
                move = min(move, max(0.0, storage - config.SIM_LEVEL_STORAGE_MIN))
            if sim_heater:
                move = min(move, max(0.0, 100.0 - heater))
            if sim_storage:
                storage -= move
            if sim_heater:
                heater += move
        else:
            # 停机后两槽缓慢回平（只搬动模拟侧的水）
            balance = (storage - heater) * config.SIM_LEVEL_BALANCE_RATE * dt
            if sim_storage and not sim_heater:
                storage -= balance          # 加热槽为实测值，只调整储水槽
            elif sim_heater and not sim_storage:
                heater += balance
            elif sim_storage and sim_heater:
                storage -= balance / 2.0
                heater += balance / 2.0

        if sim_storage:
            self.level_storage = _clamp(storage)
        if sim_heater:
            self.level_heater = _clamp(heater)

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
                # 液位通道（双水槽；当前为模拟值，传感器接入后自动切真实值）
                "level_storage": round(self.level_storage, 2),
                "level_heater": round(self.level_heater, 2),
                # 以下通道当前固件不支持，恒为 None
                "storage_temp": None,
                "heater_temp": None,
                "temperature": self.temperature,
                "pressure": self.pressure,
                "heater_state": self.heater_state,
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
        """加热模块开关：当前固件无加热通道，直接报错（保留接口以便固件升级后启用）。"""
        if not config.DEVICE_FEATURES.get("heater"):
            raise DeviceError("当前采集设备不支持加热模块控制")
        raise DeviceError("加热控制尚未实现：请先确认固件接口路径")

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
