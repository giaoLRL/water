"""全局运行时状态容器：灯杆管理器 / 告警引擎 / 设备监控 / 各灯杆活跃告警快照。

API、采集循环、告警引擎、设备监控共享同一实例 services，避免重复初始化，
并提供 lamp_alarms 等跨模块内存状态供界面实时查询。
"""
import threading
import time

import config


class Services:
    def __init__(self):
        self.lamps = None
        self.alarm = None
        self.monitor = None
        self.start_time = time.time()
        self._lock = threading.Lock()
        self.lamp_alarms = {}  # lamp_id -> list[active alarm dict]

    def set_lamp_alarms(self, lamp_id: str, alarms: list) -> None:
        with self._lock:
            self.lamp_alarms[lamp_id] = list(alarms)

    def get_lamp_alarms(self, lamp_id: str) -> list:
        with self._lock:
            return list(self.lamp_alarms.get(lamp_id, []))

    def active_count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self.lamp_alarms.values())

    @property
    def uptime(self) -> float:
        return time.time() - self.start_time

    def device_status(self) -> list[dict]:
        """按接口返回情况汇总各设备在线状态，按系统服务与灯杆分组分栏。"""
        groups = []

        # 系统服务组
        sys_items = [
            {"name": "后端服务", "type": "server", "status": "online",
             "detail": f"FastAPI · 运行 {int(self.uptime // 60)} 分钟"},
        ]
        mon = self.monitor.snapshot() if self.monitor else {}
        db_ok = mon.get("database")
        sys_items.append({
            "name": "MySQL 数据库", "type": "database",
            "status": "online" if db_ok is True else ("offline" if db_ok is False else "detecting"),
            "detail": f"{config.DB_HOST}:{config.DB_PORT}",
        })
        infer_ok = mon.get("infer")
        sys_items.append({
            "name": "AI 识别服务", "type": "infer",
            "status": "online" if infer_ok is True else ("offline" if infer_ok is False else "detecting"),
            "detail": config.INFER_URL,
        })
        groups.append({"group": "系统服务", "items": sys_items})

        # 每个灯杆一组：温湿度合并（同一 DHT11），光照、视频独立
        if self.lamps:
            for lamp in self.lamps.all():
                snap = lamp.snapshot()
                sensor_ok = snap["sensor_online"]
                light_ok = snap["light_online"]
                smoke_ok = snap.get("smoke_online")
                soil_ok = snap.get("soil_online")
                if snap["sensor_source"] == "esp32":
                    s_status = "online" if sensor_ok is True else ("offline" if sensor_ok is False else "detecting")
                    s_detail = "ESP32 + DHT11"
                    l_status = "online" if light_ok is True else ("offline" if light_ok is False else "detecting")
                    l_detail = "GY-302 (BH1750)"
                    m_status = "online" if smoke_ok is True else ("offline" if smoke_ok is False else "detecting")
                    m_detail = "MQ-2 (AO/DO)"
                    soil_status = "online" if soil_ok is True else ("offline" if soil_ok is False else "detecting")
                    soil_detail = "地面湿度传感器"
                else:
                    s_status, s_detail = "sim", "无真实传感器"
                    l_status, l_detail = "sim", "无真实传感器"
                    m_status, m_detail = "sim", "无真实传感器"
                    soil_status, soil_detail = "sim", "无真实传感器"
                v_status = "online" if snap["video_online"] else "offline"
                groups.append({
                    "group": snap["name"],
                    "items": [
                        {"name": "温湿度", "type": "sensor_temp_hum",
                         "status": s_status, "detail": s_detail},
                        {"name": "光照", "type": "sensor_lux",
                         "status": l_status, "detail": l_detail},
                        {"name": "烟雾", "type": "sensor_smoke",
                         "status": m_status, "detail": m_detail},
                        {"name": "地面湿度", "type": "sensor_soil",
                         "status": soil_status, "detail": soil_detail},
                        {"name": "视频", "type": "video",
                         "status": v_status,
                         "detail": "RTSP 实时" if snap["video_source"] == "rtsp" else "模拟画面"},
                    ],
                })
        return groups


services = Services()
