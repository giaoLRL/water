"""水循环设备模型：采集(模拟/真实接口) + 执行器(水泵/加热器)控制。

分层：业务层只调用本模块的 pump_control / heater_control / read 三个简单接口，
底层(真实继电器 HTTP、模拟物理)封装在此，业务与底层分离。

统一命名：temp_storage 储水槽温度, temp_heater 加热槽温度,
flow_rate 流量(L/min), pressure 压力(kPa), pump_state, heater_state。
"""
import json
import random
import threading
import time
import urllib.error
import urllib.request

import config


class WaterPlant:
    """单套水循环设备：2路温度+流量+压力，水泵与加热器2路继电器控制。"""

    def __init__(self):
        self._lock = threading.Lock()
        random.seed()
        # 初始安全状态：水泵关、加热关
        self.pump_state = "off"
        self.heater_state = "off"
        self.storage_temp = config.SIM_AMBIENT_TEMP
        self.heater_temp = config.SIM_AMBIENT_TEMP
        self.flow_rate = 0.0
        self.pressure = 0.0
        self.total_flow = 0.0   # 累计流量(L)，用于统计
        self._last_time = time.time()
        self.sensor_online = True   # 真实采集端在线状态(无真实端时恒真)

    # ---------- 执行器：与底层(真实继电器HTTP)解耦 ----------
    def pump_control(self, action: str) -> None:
        """水泵开关：action 为 'on'/'off'。配置了 CTRL_URL 时同时下发到ESP32继电器。"""
        if action == "on":
            self.pump_state = "on"
        else:
            self.pump_state = "off"
        self.relay_control("pump", action)

    def heater_control(self, action: str) -> None:
        """加热模块开关：action 为 'on'/'off'。配置了 CTRL_URL 时同时下发到ESP32继电器。"""
        if action == "on":
            self.heater_state = "on"
        else:
            self.heater_state = "off"
        self.relay_control("heater", action)

    def relay_control(self, device: str, action: str) -> None:
        """向真实继电器控制端下发指令(仅配 CTRL_URL 时生效)；失败不阻塞本地状态。"""
        if not config.CTRL_URL:
            return
        path = f"/{device}/{action}"
        try:
            urllib.request.urlopen(config.CTRL_URL + path, timeout=0.8).read()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 采集 ----------
    def read(self) -> dict:
        """采集一次全部传感数据(≥1Hz调用)。

        配置了 SENSOR_URL 则优先读真实采集端，读不到时退回模拟；
        未配置则直接模拟(附带水泵/加热状态)。
        """
        with self._lock:
            if config.SENSOR_URL:
                real = self.read_real()
                if real is not None:
                    # 以模拟值为底兜底，真实采集值覆盖，保证4项字段始终齐全
                    data = self._simulate()
                    data.update(real)
                    data["pump_state"] = self.pump_state
                    data["heater_state"] = self.heater_state
                    data["total_flow"] = self.total_flow
                    return data
            return self._simulate()

    def _simulate(self) -> dict:
        """模拟水路物理过程：开水泵→有流量；开加热→加热槽升温；自然散热+两槽交换。"""
        now = time.time()
        dt = min(now - self._last_time, 2.0)   # 防止长暂停后一步跳变
        self._last_time = now

        # 流量：水泵开→额定流量+扰动；关→接近0
        if self.pump_state == "on":
            self.flow_rate = max(0.0, config.SIM_FLOW_PUMP + random.uniform(-0.4, 0.4))
            self.total_flow += self.flow_rate * dt / 60.0
        else:
            self.flow_rate = max(0.0, random.uniform(0, config.SIM_FLOW_PUMP * 0.04))
            self.total_flow += self.flow_rate * dt / 60.0

        # 加热槽：开加热则升温，永远向室温自然散热
        if self.heater_state == "on":
            self.heater_temp += config.SIM_HEATER_RATE * dt
        self.heater_temp += (config.SIM_AMBIENT_TEMP - self.heater_temp) * config.SIM_COOL_RATE * dt

        # 储水槽：自然散热；水泵运行时可与加热槽缓慢交换热量
        self.storage_temp += (config.SIM_AMBIENT_TEMP - self.storage_temp) * config.SIM_COOL_RATE * dt
        if self.pump_state == "on":
            self.storage_temp += (self.heater_temp - self.storage_temp) * 0.012 * dt

        # 温度钳制防止溢出
        self.heater_temp = min(max(self.heater_temp, -5.0), 90.0)
        self.storage_temp = min(max(self.storage_temp, -5.0), 90.0)

        # 压力与流量相关(带噪声)
        self.pressure = config.SIM_PRESSURE_BASE + self.flow_rate * 4.5 + random.gauss(0, 0.5)
        self.pressure = max(0.0, self.pressure)

        return {
            "storage_temp": round(self.storage_temp + random.gauss(0, config.SIM_NOISE), 2),
            "heater_temp": round(self.heater_temp + random.gauss(0, config.SIM_NOISE), 2),
            "flow_rate": round(self.flow_rate, 2),
            "pressure": round(self.pressure, 2),
            "total_flow": round(self.total_flow, 2),
            "pump_state": self.pump_state,
            "heater_state": self.heater_state,
            "sensor_online": self.sensor_online,
        }

    # ---------- 真实接口(现场接入ESP32硬件时启用) ----------
    def read_real(self) -> dict | None:
        """从真实采集端 HTTP 读取并标准化为 4 项传感字段；未配置或无响应返回 None。

        ESP32 /api/data 常见字段名较多，这里做了别名兼容：
        储水槽温度: storage_temp/temp1/temperature_storage
        加热槽温度: heater_temp/temp2/temperature
        流量(L/min): flow_rate/flow
        压力(kPa): pressure
        数值字段不在场(如流量0)时返回缺失，由上层保留模拟值或0。
        """
        if not config.SENSOR_URL:
            return None
        try:
            with urllib.request.urlopen(config.SENSOR_URL, timeout=0.8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            self.sensor_online = False
            return None
        self.sensor_online = True
        aliases = {
            "storage_temp": ("storage_temp", "temp1", "temperature_storage"),
            "heater_temp": ("heater_temp", "temp2", "temperature"),
            "flow_rate": ("flow_rate", "flow", "flowrate"),
            "pressure": ("pressure", "pressure_kpa"),
        }
        out = {}
        for fld, keys in aliases.items():
            for k in keys:
                v = payload.get(k)
                if v is not None:
                    try:
                        out[fld] = round(float(v), 2)
                    except (TypeError, ValueError):
                        pass
                    break
        out["sensor_online"] = self.sensor_online
        return out

    def control_real(self, device: str, action: str) -> None:
        """兼容旧接口：继电器下发已并入 relay_control，此处仅作转发。"""
        self.relay_control(device, action)