"""水循环异常告警引擎：按阈值检查各采集通道，超限/恢复自动入库并更新活跃态。

通道由 config.DEVICE_FEATURES 决定：流量/温度×2/压力/光照通道均已启用；
后续新增通道只需在 CHANNELS 加一行并在 DEVICE_FEATURES 声明能力即可，无需改检查逻辑。

阈值在"系统配置"页可改，存数据库 config 表覆盖 config.py 默认值。
读数无效（None，即设备离线或该通道无数据）时不产生告警，避免误报。

报警联动（v3：卡片即来源）：规则自带阈值，触发源与动作都**直接选自实时监控页的卡片**
（内置卡或后加的自定义卡），不再有独立的执行器档案/通道声明：
  - 触发源 source_card：卡片 source.kind=realtime 直接读采集快照；kind=custom
    （自定义接口卡）由 custom_channels 轮询后并入 data["custom"]，卡片已不在布局中时
    由 check_custom 按保存时的快照 URL 兜底轮询；
  - 动作 {card, state, kind, url}：卡片当前配置优先（改了卡片规则自动跟随），
    kind=url（控制卡自定义指令，走白名单 GET）/pump/heater（内置泵、加热）/quant（取消定量）。
规则存 config 表 sys.alarm_links，系统配置页编辑；每个动作写 control_log(link_*，source=auto)。
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
# 触发源取值方式：realtime = 本机采集快照；custom = 自定义接口卡（后端按卡片周期轮询）
LINK_SOURCE_KINDS = ("realtime", "custom")
# 动作执行方式：url = 卡片自定义开/关指令；pump/heater = 内置执行器；quant = 取消定量
LINK_TARGET_KINDS = ("url", "pump", "heater", "quant")


class AlarmEngine:
    """简单阈值告警引擎(单套系统，无设备维度)。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._thresholds: dict[str, float] = {}
        self._active: dict[str, dict] = {}   # type -> 告警项
        self.plant = None                    # 联动动作的执行对象（main 启动时注入 WaterPlant）
        self._links: list[dict] = []         # 报警联动规则（v3：卡片即来源）
        self._cards: dict[str, dict] = {}    # 仪表盘卡片 id -> 卡片（规则解析用）
        self._link_state: dict[str, bool] = {}     # 规则id -> 当前是否越限（触发/恢复沿判断）
        self._custom_ts: dict[str, float] = {}     # 规则id -> 自定义源上次轮询时间
        self.reload_links()
        self.reload()

    def reload_links(self) -> None:
        """从 config 表读取联动规则（sys.alarm_links）并缓存仪表盘卡片。

        规则必须带 source_card（v3 卡片即来源）；旧版规则（v1 无 sensor、v2 走档案）直接丢弃
        ——本项目无生产规则数据，不做迁移。
        """
        try:
            links = store.get_json("alarm_links", [])
        except Exception:  # noqa: BLE001
            links = []
        try:
            layout = store.get_json("dashboard_layout", None)
        except Exception:  # noqa: BLE001
            layout = None
        widgets = (layout or {}).get("widgets") if isinstance(layout, dict) else None
        cards = {str(w["id"]): w for w in (widgets or [])
                 if isinstance(w, dict) and w.get("id")}
        with self._lock:
            self._links = [r for r in links
                           if isinstance(r, dict) and r.get("source_card")
                           and isinstance(r.get("source"), dict)]
            self._cards = cards

    def links(self) -> list[dict]:
        """当前联动规则（供系统配置接口返回）。"""
        with self._lock:
            return list(self._links)

    def _card(self, card_id) -> dict | None:
        with self._lock:
            return self._cards.get(str(card_id or ""))

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
        """联动自定义源兜底轮询（采集循环每周期调用一次，内部按各自 period 节流）。

        卡片仍在实时监控页时，读数由 custom_channels 轮询并并入 data["custom"]，这里跳过；
        卡片已被删除但规则仍引用它时，按规则保存时的快照 URL 继续轮询，避免规则静默失效。
        """
        now = time.time()
        with self._lock:
            tasks = []
            for rule in self._links:
                if not rule.get("enabled"):
                    continue
                src = self._rule_source(rule)
                if src.get("kind") != "custom" or self._card(rule.get("source_card")):
                    continue
                try:
                    period = max(2.0, float(src.get("period") or 5))
                except (TypeError, ValueError):
                    period = 5.0
                if now - self._custom_ts.get(rule["id"], 0.0) < period:
                    continue
                tasks.append(rule)
                self._custom_ts[rule["id"]] = now   # 先占位：请求慢也不会每周期重复打
        for rule in tasks:
            src = self._rule_source(rule)
            value = self._fetch_sensor(src.get("url", ""), src.get("path", ""))
            if value is not None:
                self._eval_link(rule, value)

    def _rule_source(self, rule: dict) -> dict:
        """规则触发源：卡片当前配置优先（改卡片规则自动跟随），卡片不在布局时回退保存时快照。"""
        card = self._card(rule.get("source_card"))
        src = (card or {}).get("source")
        return src if isinstance(src, dict) else (rule.get("source") or {})

    @staticmethod
    def _numeric(value):
        """读数归一化为 float：水槽卡等对象源取 percent；非数值返回 None。"""
        if isinstance(value, dict):
            value = value.get("percent")
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _resolve_action(self, act: dict) -> dict:
        """动作目标解析：卡片当前配置优先，卡片不在布局时回退保存时快照。"""
        out = {"card": act.get("card"), "state": act.get("state") or "off",
               "kind": act.get("kind"), "url": act.get("url") or "", "title": ""}
        card = self._card(act.get("card"))
        if not isinstance(card, dict):
            return out
        out["title"] = card.get("title") or ""
        cmd = card.get("cmd") or {}
        url = cmd.get("on") if out["state"] == "on" else cmd.get("off")
        if url:
            out["kind"], out["url"] = "url", url
        elif card.get("builtin") == "quant":
            out["kind"], out["state"] = "quant", "off"
        elif card.get("ctl") == "heater":
            out["kind"] = "heater"
        elif card.get("ctl") == "pump":
            out["kind"] = "pump"
        return out

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
        """联动评估（随采集循环）：按规则触发源卡片的来源取值。

        realtime 源直接读本机采集快照（路径取自卡片）；custom 源读 custom_channels 的轮询结果
        （卡片已不在布局中时交给 check_custom 兜底，这里跳过）。
        """
        with self._lock:
            rules = [r for r in self._links if r.get("enabled")]
        for rule in rules:
            src = self._rule_source(rule)
            kind = src.get("kind")
            if kind == "realtime":
                raw = self._get_path(data, src.get("path", ""))
            elif kind == "custom":
                raw = (data.get("custom") or {}).get(str(rule.get("source_card") or ""))
            else:
                continue
            if raw is None:
                continue
            value = self._numeric(raw)
            if value is None:
                continue
            self._eval_link(rule, value)

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
        prefix = (f'联动[{ctx["name"]}]: {ctx["text"]}'
                  f'({ctx["value"]:.2f} / 阈值{ctx["threshold"]}) {phase}')
        try:
            target = self._resolve_action(act)
            kind = str(target.get("kind") or "")
            state = "on" if target.get("state") == "on" else "off"
            label = target.get("title") or target.get("card") or "卡片"
            if kind == "url":
                url = str(target.get("url") or "")
                if not url:
                    raise ValueError(f"卡片[{label}] 未配置该方向的指令 URL")
                self._http_get(url)
                detail, action = f"卡片[{label}] {state.upper()} {url[:140]}", f"link_card_{state}"
            elif kind == "pump":
                self.plant.pump_control(state)
                detail, action = f"卡片[{label}] 本机水泵 {state.upper()}", f"link_pump_{state}"
            elif kind == "heater":
                self.plant.heater_control(state)
                detail, action = f"卡片[{label}] 本机加热 {state.upper()}", f"link_heater_{state}"
            elif kind == "quant":
                self.plant.set_pump_target(0)
                detail, action = f"卡片[{label}] 取消定量浇水", "link_quant_cancel"
            else:
                raise ValueError(f"非法联动动作: {kind or act}")
            database.insert_control_log(action, "success", f"{prefix} → {detail}", source="auto")
        except Exception as exc:  # noqa: BLE001
            database.insert_control_log("link_fail", "fail",
                                        f"{prefix} → 动作执行失败: {exc}"[:250], source="auto")

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
