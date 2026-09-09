"""水循环异常告警引擎：按温度/流量/压力阈值检查，超限/恢复自动入库并更新活跃态。

类型：storage_temp 储水槽温度, heater_temp 加热槽温度, flow_rate 流量, pressure 压力。
阈值在"系统配置"页可改，存数据库 config 表覆盖 config.py 默认值。
"""
import threading
from datetime import datetime

import config
import database


class AlarmEngine:
    """简单阈值告警引擎(单套系统，无设备维度)。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._thresholds: dict[str, float] = {}
        self._active: dict[str, dict] = {}   # type -> 告警项
        self.reload()

    def reload(self) -> None:
        """从数据库读取最新阈值(覆盖默认值)。"""
        cfg = database.get_all_config()
        with self._lock:
            self._thresholds = {
                key: float(cfg.get(key, str(default)))
                for key, default in config.DEFAULT_THRESHOLDS.items()
            }

    @property
    def thresholds(self) -> dict:
        with self._lock:
            return dict(self._thresholds)

    def check(self, data: dict) -> list[dict]:
        """检查一次采样，返回当前全部活跃告警；处理新告警入库与恢复。

        data 需含 storage_temp/heater_temp/flow_rate/pressure 四个读数。
        """
        with self._lock:
            checks = [
                ("storage_temp", "储水槽温度",    "storage_temp_max", "storage_temp_min"),
                ("heater_temp",  "加热槽温度",    "heater_temp_max",  "heater_temp_min"),
                ("flow_rate",    "水流量",        "flow_max",         "flow_min"),
                ("pressure",     "水压",          "pressure_max",     "pressure_min"),
            ]
            active_now: dict[str, dict] = {}
            for key, label, hi_key, lo_key in checks:
                value = float(data.get(key, 0.0))
                low = self._thresholds[lo_key]
                high = self._thresholds[hi_key]
                if value > high:
                    active_now[key] = {"label": label, "value": value,
                                       "threshold": high, "direction": "above"}
                elif value < low:
                    active_now[key] = {"label": label, "value": value,
                                       "threshold": low, "direction": "below"}

            # 恢复已回正常范围的告警
            for key in list(self._active):
                if key not in active_now:
                    database.recover_alarm(key)
                    del self._active[key]

            # 新告警入库并更新活跃态
            for key, item in active_now.items():
                if key not in self._active:
                    text = "超上限" if item["direction"] == "above" else "低于下限"
                    database.update_or_insert_alarm(
                        datetime.now(), key,
                        round(item["value"], 2), item["threshold"],
                        item["direction"],
                        f'{item["label"]} {item["value"]:.2f} {text} {item["threshold"]}',
                    )
                self._active[key] = item

            return [
                {"type": k, "label": v["label"], "value": round(v["value"], 2),
                 "threshold": v["threshold"], "direction": v["direction"]}
                for k, v in active_now.items()
            ]

    def active_alarms(self) -> list[dict]:
        """当前活跃告警(供实时接口返回)。"""
        with self._lock:
            return [
                {"type": k, "label": v["label"], "value": round(v["value"], 2),
                 "threshold": v["threshold"], "direction": v["direction"]}
                for k, v in self._active.items()
            ]