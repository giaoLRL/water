"""灯杆异常告警引擎：按温度 / 湿度 / 光照阈值检查环境参数，并按自定义人数规则检查人员告警。

- 环境参数：由采集循环周期调用 check() 检查阈值，超限/恢复自动入库并更新状态；
- 人数告警：持续识别线程把检测人数实时写入 check_person()，规则（启用 + 阈值）可在前端自定义；
- 新告警可携带异常快照图（人员标注图 / 环境帧），供报警记录页点开查看；
- 多线程安全：采集已改为线程池并行，内部状态通过可重入锁保护。
"""
import threading
from datetime import datetime

import config
import database


class AlarmEngine:
    """告警引擎。

    collect_loop 已改为线程池并行采集（3 个灯杆线程 + 持续识别线程都会调用本引擎），
    因此 _active / _thresholds 的读写均通过可重入锁保护。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._thresholds = {}
        self._active = {}  # (lamp_id, type) -> item
        self.reload()

    def reload(self) -> None:
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

    def check(self, lamp_id: str, sensors: dict, image: str | None = None) -> list[dict]:
        """检查一次采样，返回该灯杆当前全部活跃告警；处理新告警入库（携带异常快照图）与恢复。"""
        with self._lock:
            return self._check_locked(lamp_id, sensors, image)

    def _check_locked(self, lamp_id: str, sensors: dict, image: str | None) -> list[dict]:
        checks = [
            ("temperature", "环境温度", sensors.get("temperature", 0.0), "temp_max", "temp_min"),
            ("humidity", "空气湿度", sensors.get("humidity", 0.0), "humidity_max", "humidity_min"),
            ("luminance", "光照强度", sensors.get("luminance", 0.0), "luminance_max", "luminance_min"),
        ]
        active_now: dict[str, dict] = {}

        for key, label, value, hi_key, lo_key in checks:
            low, high = self._thresholds[lo_key], self._thresholds[hi_key]
            direction = None
            threshold = None
            if value > high:
                direction, threshold = "above", high
            elif value < low:
                direction, threshold = "below", low

            if direction:
                active_now[key] = {
                    "lamp_id": lamp_id,
                    "type": key,
                    "label": label,
                    "value": round(float(value), 2),
                    "threshold": threshold,
                    "direction": direction,
                }

        # 烟雾告警：由 ESP32 的 DO 电平（smoke_alarm）判断，非阈值比较
        if sensors.get("smoke_alarm"):
            smoke_val = round(float(sensors.get("smoke", 0.0) or 0.0), 2)
            active_now["smoke"] = {
                "lamp_id": lamp_id,
                "type": "smoke",
                "label": "烟雾浓度",
                "value": smoke_val,
                "threshold": 1.0,
                "direction": "above",
                "message": f"检测到烟雾超标，浓度 {smoke_val}",
            }

        # 恢复已回正常范围的告警（人员告警由 check_person 单独管理，此处跳过）
        for (lid, key) in list(self._active):
            if lid == lamp_id and key != "person" and key not in active_now:
                database.recover_alarm(lid, key)

        # 新告警入库（携带异常快照图；同类型活跃期间只保留一条 active 记录）
        for key, item in active_now.items():
            if (lamp_id, key) not in self._active:
                direction_text = "超上限" if item["direction"] == "above" else "低于下限"
                message = item.get("message") or f'{item["label"]} {item["value"]} {direction_text} {item["threshold"]}'
                database.update_or_insert_alarm(
                    lamp_id,
                    datetime.now(),
                    item["type"],
                    item["value"],
                    item["threshold"],
                    item["direction"],
                    message,
                    image=image,
                )

        # 更新活跃集合（保留人员活跃项，便于本方法将其并入快照）
        for lid_key in list(self._active):
            if lid_key[0] == lamp_id and lid_key[1] != "person":
                del self._active[lid_key]
        for key, item in active_now.items():
            self._active[(lamp_id, key)] = item

        new_alarms = list(active_now.values())
        # 补充人员数量告警（由持续识别线程实时写入，此处归并到快照）
        new_alarms.extend(self._person_active_locked(lamp_id))

        # 兜底恢复：本轮不活跃的环境类型，把数据库中残留的 active 记录置为已恢复，
        # 保证进程重启后数据库 status 与内存真实活跃一致（幂等、每轮执行开销极小）。
        not_active = [t for t in ("temperature", "humidity", "luminance", "smoke") if t not in active_now]
        if not_active:
            database.recover_alarms_for_types(lamp_id, not_active)
        return new_alarms

    def check_person(self, lamp_id: str, person_count: int, image: str | None = None) -> list[dict]:
        """按前端自定义规则检查人数告警；返回该灯杆当前人员活跃告警。"""
        with self._lock:
            enabled = bool(self._thresholds.get(
                "person_alert_enabled", config.DEFAULT_THRESHOLDS["person_alert_enabled"]))
            threshold = float(self._thresholds.get(
                "person_alert_min", config.DEFAULT_THRESHOLDS["person_alert_min"]))
            if not enabled or person_count < threshold:
                # 规则关闭或人数回落：恢复已有人员告警
                if (lamp_id, "person") in self._active:
                    database.recover_alarm(lamp_id, "person")
                    del self._active[(lamp_id, "person")]
                return []
            item = {
                "lamp_id": lamp_id,
                "type": "person",
                "label": "人员数量",
                "value": round(float(person_count), 2),
                "threshold": threshold,
                "direction": "above",
            }
            if (lamp_id, "person") not in self._active:
                database.update_or_insert_alarm(
                    lamp_id, datetime.now(), "person", person_count, threshold, "above",
                    f"检测到 {int(person_count)} 人，超过告警阈值 {int(threshold)} 人",
                    image=image,
                )
                self._active[(lamp_id, "person")] = item
            return [item]

    def person_active(self, lamp_id: str) -> list[dict]:
        """返回某灯杆当前人员数量的活跃告警项。"""
        with self._lock:
            return self._person_active_locked(lamp_id)

    def _person_active_locked(self, lamp_id: str) -> list[dict]:
        return [it for (lid, t), it in self._active.items() if lid == lamp_id and t == "person"]