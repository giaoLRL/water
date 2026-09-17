"""水循环异常告警引擎：按阈值检查各采集通道，超限/恢复自动入库并更新活跃态。

通道由 config.DEVICE_FEATURES 决定：流量/温度×2/压力/光照通道均已启用；
后续新增通道只需在 CHANNELS 加一行并在 DEVICE_FEATURES 声明能力即可，无需改检查逻辑。

阈值在"系统配置"页可改，存数据库 config 表覆盖 config.py 默认值。
读数无效（None，即设备离线或该通道无数据）时不产生告警，避免误报。
"""
import threading
from datetime import datetime

import config
import database

# (数据字段, 显示名, 上限阈值键, 下限阈值键, 依赖的设备能力)
CHANNELS = (
    ("flow_rate",    "水流量",     "flow_max",         "flow_min",         "flow"),
    ("storage_temp", "储水槽温度", "storage_temp_max", "storage_temp_min", "temperature"),
    ("heater_temp",  "加热槽温度", "heater_temp_max",  "heater_temp_min",  "temperature"),
    ("pressure",     "水压",       "pressure_max",     "pressure_min",     "pressure"),
    ("light",        "光照",       "light_max",        "light_min",        "light"),
)


class AlarmEngine:
    """简单阈值告警引擎(单套系统，无设备维度)。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._thresholds: dict[str, float] = {}
        self._active: dict[str, dict] = {}   # type -> 告警项
        self.reload()

    def reload(self) -> None:
        """从数据库读取最新阈值(覆盖默认值)；非法值回退默认值。"""
        cfg = database.get_all_config()
        with self._lock:
            thresholds: dict[str, float] = {}
            for key, default in config.DEFAULT_THRESHOLDS.items():
                try:
                    thresholds[key] = float(cfg.get(key, str(default)))
                except (TypeError, ValueError):
                    thresholds[key] = float(default)
            self._thresholds = thresholds

    @property
    def thresholds(self) -> dict:
        with self._lock:
            return dict(self._thresholds)

    def _enabled_channels(self) -> list[tuple]:
        """当前设备实际支持的告警通道。"""
        return [c for c in CHANNELS if config.DEVICE_FEATURES.get(c[4])]

    def check(self, data: dict) -> list[dict]:
        """检查一次采样，返回当前全部活跃告警；处理新告警入库与恢复。

        data 中值为 None 的通道视为“无数据”，跳过检查（不误报）。
        """
        with self._lock:
            active_now: dict[str, dict] = {}
            for key, label, hi_key, lo_key, _feature in self._enabled_channels():
                raw = data.get(key)
                if raw is None:
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                low = self._thresholds.get(lo_key)
                high = self._thresholds.get(hi_key)
                if high is not None and value > high:
                    active_now[key] = {"label": label, "value": value,
                                       "threshold": high, "direction": "above"}
                elif low is not None and value < low:
                    active_now[key] = {"label": label, "value": value,
                                       "threshold": low, "direction": "below"}

            # 恢复已回正常范围(或该通道已不可用)的告警
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
