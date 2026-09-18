"""水循环异常告警引擎：按阈值检查各采集通道，超限/恢复自动入库并更新活跃态。

通道由 config.DEVICE_FEATURES 决定：流量/温度×2/压力/光照通道均已启用；
后续新增通道只需在 CHANNELS 加一行并在 DEVICE_FEATURES 声明能力即可，无需改检查逻辑。

阈值在"系统配置"页可改，存数据库 config 表覆盖 config.py 默认值。
读数无效（None，即设备离线或该通道无数据）时不产生告警，避免误报。

报警联动（v2 个体化）：规则自带阈值，触发源 = 本机传感器（CHANNELS）或自定义接口传感器
（URL+取值路径+轮询周期）；动作指向明确的执行器个体——本机水泵/本机加热/取消定量，
或执行器档案中的外部装置（开/关 GET URL，如水阀）。可选恢复动作：读数回到正常区间后自动执行。
规则与执行器档案存 config 表 sys.alarm_links / sys.actuators，系统配置页编辑；
每个动作写 control_log(link_*，source=auto)。
"""
import threading
import time
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

import config
import database
import store

# (数据字段, 显示名, 上限阈值键, 下限阈值键, 依赖的设备能力)
CHANNELS = (
    ("flow_rate",    "水流量",     "flow_max",         "flow_min",         "flow"),
    ("storage_temp", "储水槽温度", "storage_temp_max", "storage_temp_min", "temperature"),
    ("heater_temp",  "加热槽温度", "heater_temp_max",  "heater_temp_min",  "temperature"),
    ("pressure",     "水压",       "pressure_max",     "pressure_min",     "pressure"),
    ("light",        "光照",       "light_max",        "light_min",        "light"),
)

# 联动合法通道与方向
LINK_CHANNELS = {c[0] for c in CHANNELS}
LINK_DIRECTIONS = ("above", "below")
# 联动动作类型：off_pump 关本机水泵 / off_heater 关本机加热 / cancel_target 取消定量 /
# actuator 执行器档案中的外部装置（按 state 开/关）
LINK_ACTION_TYPES = ("off_pump", "off_heater", "cancel_target", "actuator")
LINK_SENSOR_KINDS = ("builtin", "custom", "channel")   # channel = 自定义通道（全链路声明）


class AlarmEngine:
    """简单阈值告警引擎(单套系统，无设备维度)。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._thresholds: dict[str, float] = {}
        self._active: dict[str, dict] = {}   # type -> 告警项
        self.plant = None                    # 联动动作的执行对象（main 启动时注入 WaterPlant）
        self._links: list[dict] = []         # 报警联动规则（v2 个体化）
        self._actuators: list[dict] = []     # 执行器档案（外部装置：名称+开/关 URL）
        self._link_state: dict[str, bool] = {}     # 规则id -> 当前是否越限（触发/恢复沿判断）
        self._custom_ts: dict[str, float] = {}     # 规则id -> 自定义源上次轮询时间
        self.reload_links()
        self.reload()

    def reload_links(self) -> None:
        """从 config 表读取联动规则（sys.alarm_links）与执行器档案（sys.actuators）。

        旧版（v1，无 sensor 字段）规则直接丢弃——本项目无生产规则数据，不做迁移。
        """
        try:
            links = store.get_json("alarm_links", [])
        except Exception:  # noqa: BLE001
            links = []
        try:
            actuators = store.get_json("actuators", [])
        except Exception:  # noqa: BLE001
            actuators = []
        with self._lock:
            self._links = [r for r in links
                           if isinstance(r, dict) and isinstance(r.get("sensor"), dict)]
            self._actuators = actuators if isinstance(actuators, list) else []

    def links(self) -> list[dict]:
        """当前联动规则（供系统配置接口返回）。"""
        with self._lock:
            return list(self._links)

    def actuators(self) -> list[dict]:
        """当前执行器档案（供系统配置接口返回）。"""
        with self._lock:
            return list(self._actuators)

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

            snapshot = [
                {"type": k, "label": v["label"], "value": round(v["value"], 2),
                 "threshold": v["threshold"], "direction": v["direction"]}
                for k, v in active_now.items()
            ]

        # 联动评估在锁外执行：动作/轮询含网络请求，避免阻塞 active_alarms 读
        self._eval_builtin_links(data)
        return snapshot

    def check_custom(self) -> None:
        """联动自定义传感器源轮询（采集循环每周期调用一次，内部按各自 period 节流）。"""
        now = time.time()
        with self._lock:
            tasks = []
            for rule in self._links:
                sensor = rule.get("sensor") or {}
                if not rule.get("enabled") or sensor.get("kind") != "custom":
                    continue
                period = max(2.0, float(sensor.get("period") or 5))
                if now - self._custom_ts.get(rule["id"], 0.0) < period:
                    continue
                tasks.append(rule)
                self._custom_ts[rule["id"]] = now   # 先占位：请求慢也不会每周期重复打
        for rule in tasks:
            sensor = rule["sensor"]
            value = self._fetch_sensor(sensor.get("url", ""), sensor.get("path", ""))
            if value is not None:
                self._eval_link(rule, value)

    def active_alarms(self) -> list[dict]:
        """当前活跃告警(供实时接口返回)。"""
        with self._lock:
            return [
                {"type": k, "label": v["label"], "value": round(v["value"], 2),
                 "threshold": v["threshold"], "direction": v["direction"]}
                for k, v in self._active.items()
            ]

    # ---------- 报警联动（v2 个体化） ----------
    @staticmethod
    def _get_path(obj, path: str):
        """按 a.b.c 路径取值，取不到返回 None。"""
        try:
            for k in str(path).split("."):
                obj = obj[k] if isinstance(obj, dict) else None
            return obj
        except (KeyError, TypeError):
            return None

    def _fetch_sensor(self, url: str, path: str):
        """拉取自定义传感器读数：白名单校验 → GET → JSON 取值路径 → float；失败返回 None。"""
        import json as _json
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return None
        if u.hostname not in config.DASHBOARD_PROXY_HOSTS:
            return None
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=config.DEVICE_TIMEOUT_S) as resp:
                payload = _json.loads(resp.read(131_072).decode("utf-8", "replace"))
            raw = self._get_path(payload, path)
            return None if raw is None else float(raw)
        except Exception:  # noqa: BLE001
            return None

    def _eval_builtin_links(self, data: dict) -> None:
        """联动评估（随采集循环）：本机通道取 data 字段；自定义通道取 data["custom"][id]。"""
        with self._lock:
            rules = [r for r in self._links if r.get("enabled")]
        for rule in rules:
            sensor = rule.get("sensor") or {}
            if sensor.get("kind") == "channel":
                value = (data.get("custom") or {}).get(sensor.get("channel_id"))
            elif sensor.get("kind") == "builtin":
                value = data.get(sensor.get("channel"))
            else:
                continue   # custom（裸 URL）由 check_custom 轮询
            if value is None:
                continue
            try:
                self._eval_link(rule, float(value))
            except (TypeError, ValueError):
                continue

    def _eval_link(self, rule: dict, value: float) -> None:
        """单规则评估：越限沿（正常→越限）触发动作，回正常沿执行可选恢复动作。"""
        try:
            threshold = float(rule.get("threshold"))
        except (TypeError, ValueError):
            return
        above = rule.get("direction") == "above"
        tripped = value > threshold if above else value < threshold
        rid = rule["id"]
        with self._lock:
            prev = self._link_state.get(rid, False)
            self._link_state[rid] = tripped
        if tripped == prev:
            return
        text = "超上限" if above else "低于下限"
        ctx = {"name": rule.get("name") or rid, "value": value, "threshold": threshold,
               "direction": rule.get("direction"), "text": text}
        if tripped:
            # 触发沿：写告警记录（告警记录页可见，type=custom:<规则id>）并执行动作
            database.update_or_insert_alarm(
                datetime.now(), f"custom:{rid}",
                round(value, 2), threshold, rule.get("direction"),
                f'{rule.get("name") or rid} {value:.2f} {text} {threshold}',
            )
            for act in rule.get("actions") or []:
                self._exec_action(rule, act, ctx, "触发")
        else:
            database.recover_alarm(f"custom:{rid}")
            if rule.get("recover", {}).get("actions"):
                for act in rule["recover"]["actions"]:
                    self._exec_action(rule, act, ctx, "恢复")

    def _exec_action(self, rule: dict, act: dict, ctx: dict, phase: str) -> None:
        """执行单个联动动作并写审计日志；任何失败都只记 fail 不抛出。"""
        atype = str(act.get("type", ""))
        prefix = (f'联动[{ctx["name"]}]: {ctx["text"]}'
                  f'({ctx["value"]:.2f} / 阈值{ctx["threshold"]}) {phase}')
        try:
            if atype == "off_pump":
                self.plant.pump_control("off")
                database.insert_control_log("link_off_pump", "success",
                                            f"{prefix} → 本机水泵 关", source="auto")
            elif atype == "off_heater":
                self.plant.heater_control("off")
                database.insert_control_log("link_off_heater", "success",
                                            f"{prefix} → 本机加热 关", source="auto")
            elif atype == "cancel_target":
                self.plant.set_pump_target(0)
                database.insert_control_log("link_cancel_target", "success",
                                            f"{prefix} → 取消定量", source="auto")
            elif atype == "actuator":
                dev = self._find_actuator(act.get("actuator"))
                state = "on" if act.get("state") == "on" else "off"
                url = str(dev.get(f"{state}_url", "") or "")
                if not url:
                    raise ValueError(f"执行器[{dev.get('name')}] 未配置{'开' if state == 'on' else '关'}指令 URL")
                self._http_get(url)
                database.insert_control_log("link_actuator", "success",
                                            f"{prefix} → 执行器[{dev.get('name')}] {state.upper()} {url[:140]}",
                                            source="auto")
            else:
                return
        except Exception as exc:  # noqa: BLE001
            database.insert_control_log(
                f"link_{atype}" if atype in LINK_ACTION_TYPES else "link_unknown",
                "fail", f"{prefix} → 动作执行失败: {exc}"[:250], source="auto")

    def _find_actuator(self, actuator_id) -> dict:
        with self._lock:
            for a in self._actuators:
                if a.get("id") == actuator_id:
                    return a
        raise ValueError(f"执行器档案不存在: {actuator_id}")

    @staticmethod
    def _http_get(url: str) -> None:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("非法 URL（仅支持 http/https）")
        if u.hostname not in config.DASHBOARD_PROXY_HOSTS:
            raise ValueError(f"目标主机不在白名单: {u.hostname}")
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=config.DEVICE_TIMEOUT_S) as resp:
            resp.read(65536)
