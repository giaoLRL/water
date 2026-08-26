"""全局运行时状态容器，供 API、采集循环、告警引擎、设备监控共享。"""
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
        """按接口返回情况汇总各设备在线状态。"""
        items = [
            {"name": "后端服务", "type": "server", "status": "online",
             "detail": f"FastAPI · 运行 {int(self.uptime // 60)} 分钟"},
        ]
        mon = self.monitor.snapshot() if self.monitor else {}
        db_ok = mon.get("database")
        items.append({
            "name": "MySQL 数据库", "type": "database",
            "status": "online" if db_ok is True else ("offline" if db_ok is False else "detecting"),
            "detail": f"{config.DB_HOST}:{config.DB_PORT}",
        })
        infer_ok = mon.get("infer")
        items.append({
            "name": "AI 识别服务", "type": "infer",
            "status": "online" if infer_ok is True else ("offline" if infer_ok is False else "detecting"),
            "detail": config.INFER_URL,
        })
        if self.lamps:
            for lamp in self.lamps.all():
                snap = lamp.snapshot()
                sensor_ok = snap["sensor_online"]
                light_ok = snap["light_online"]
                if snap["sensor_source"] == "esp32":
                    s_status = "online" if sensor_ok is True else ("offline" if sensor_ok is False else "detecting")
                    s_detail = "ESP32 + DHT11"
                    # 光照传感器（GY-302）单独作为健康指标
                    l_status = "online" if light_ok is True else ("offline" if light_ok is False else "detecting")
                    l_detail = "GY-302 (BH1750)"
                else:
                    s_status, s_detail = "sim", "无真实传感器"
                    l_status, l_detail = "sim", "无真实传感器"
                # 传感器按三个指标分别呈现健康状态
                items.append({"name": f"{snap['name']} 温度", "type": "sensor_temp",
                              "status": s_status, "detail": s_detail})
                items.append({"name": f"{snap['name']} 湿度", "type": "sensor_hum",
                              "status": s_status, "detail": s_detail})
                items.append({"name": f"{snap['name']} 光照", "type": "sensor_lux",
                              "status": l_status, "detail": l_detail})
                v_status = "online" if snap["video_online"] else "offline"
                items.append({"name": f"{snap['name']} 视频", "type": "video",
                              "status": v_status,
                              "detail": "RTSP 实时" if snap["video_source"] == "rtsp" else "模拟画面"})
        return items


services = Services()