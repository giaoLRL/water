"""恒温闭环回路管理器（卡片即来源）：**一张「恒温闭环」卡 = 一路独立 PID**。

回路配置写在卡片上（sys.dashboard_layout 里 builtin=pid 的卡片，字段 pid）：
  pid: {sensor_card, sensor_source{kind,url,path,period},
        actuator_card, actuator{kind,url},
        guard_card, guard_source{...}, guard_min,
        target, kp, ki, kd, enabled}

- 温度来源 = 该卡片 source：kind=realtime → 从本轮采集快照按路径取值；
  kind=custom → 取该自定义通道的轮询结果（卡片被删则跳过该回路并在状态里给原因）；
- 加热执行器 = 该卡片：内置加热卡 → 本机继电器；内置水泵卡 → 本机水泵；
  自定义控制卡 → 按开/关指令 URL 走白名单 GET（与报警联动同一套规则）；
- 前置条件（可选，防干烧）= guard_card 的值必须 ≥ guard_min，否则强制断开加热并跳过 PID；
- 温度无有效读数时跳过本轮（不清零输出、不误动作）。

多水槽场景：加几张闭环卡就是几路独立闭环（各自一份 PID 状态：积分/上次误差/上次占空比）。
**没有配置任何闭环卡时回退到"默认回路"**（加热槽温度 → 本机加热，参数取 sys.pid_*），
与旧版单回路行为完全一致。
"""
import threading
import time
import urllib.request
from urllib.parse import urlparse

import config
import database
import store
from pid_control import PID, heater_action


def _get_path(obj, path: str):
    """按 a.b.c 路径取值，取不到返回 None。"""
    try:
        for k in str(path).split("."):
            obj = obj[k] if isinstance(obj, dict) else None
        return obj
    except (KeyError, TypeError):
        return None


def _numeric(value):
    """读数归一化为 float：水槽卡等对象源取 percent；非数值返回 None。"""
    if isinstance(value, dict):
        value = value.get("percent")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PidLoopManager:
    """从仪表盘布局派生闭环回路，按周期执行 PID → 加热开/关。"""

    def __init__(self, plant=None):
        self._lock = threading.Lock()
        self.plant = plant                  # WaterPlant（内置加热/水泵执行）
        self._loops: list[dict] = []        # 卡片派生的回路
        self._cards: dict[str, dict] = {}
        self._pids: dict[str, PID] = {}     # 回路id -> PID 实例（保留积分状态）
        self._last_action: dict[str, str] = {}
        self.reload()

    # ---------- 配置 ----------
    def reload(self) -> None:
        """重新从布局派生回路（布局保存后调用；卡片被删则该回路消失）。"""
        try:
            layout = store.get_json("dashboard_layout", None) or {}
        except Exception:  # noqa: BLE001
            layout = {}
        widgets = layout.get("widgets") if isinstance(layout, dict) else None
        cards = {str(w["id"]): w for w in (widgets or [])
                 if isinstance(w, dict) and w.get("id")}
        loops = []
        for w in (widgets or []):
            if not isinstance(w, dict) or w.get("builtin") != "pid":
                continue
            cfg = w.get("pid") if isinstance(w.get("pid"), dict) else {}
            loops.append({"id": str(w["id"]), "name": w.get("title") or str(w["id"]),
                          "cfg": cfg, "measured": None, "duty": 0.0, "action": "",
                          "error": "", "guard_ok": True})
        with self._lock:
            self._cards = cards
            ids = {l["id"] for l in loops}
            self._pids = {k: v for k, v in self._pids.items() if k in ids}
            self._last_action = {k: v for k, v in self._last_action.items() if k in ids}
            self._loops = loops

    def loops(self) -> list[dict]:
        """回路运行状态（含来源卡/执行器卡/前置条件卡名称，供卡片显示）。"""
        with self._lock:
            cards = dict(self._cards)
            raw = [{k: v for k, v in l.items() if k != "cfg"} for l in self._loops]
            cfgs = [dict(l["cfg"] or {}) for l in self._loops]
        out = []
        for item, cfg in zip(raw, cfgs):
            legacy = not cfg.get("sensor_card") and not cfg.get("actuator_card")
            spec = self._resolve_actuator(cfg) if not legacy else {"kind": "heater", "title": "本机加热"}
            sensor_card = str(cfg.get("sensor_card") or "")
            guard_card = str(cfg.get("guard_card") or "")
            item.update({
                "legacy": legacy,
                "sensor_card": sensor_card,
                "sensor_title": (cards.get(sensor_card) or {}).get("title") or
                                ("加热槽水温（默认）" if legacy else ""),
                "actuator_card": str(cfg.get("actuator_card") or ""),
                "actuator_title": spec.get("title") or ("本机加热（默认）" if legacy else ""),
                "actuator_kind": spec.get("kind") or "heater",
                "guard_card": guard_card,
                "guard_title": (cards.get(guard_card) or {}).get("title") or "",
                "guard_min": cfg.get("guard_min"),
                "target": cfg.get("target"),
                "kp": cfg.get("kp"), "ki": cfg.get("ki"), "kd": cfg.get("kd"),
                # 老式卡未在卡上存 enabled 时用全局开关，界面显示的是"实际是否在跑"
                "enabled": (bool(store.pid_enabled()) if cfg.get("enabled") is None else bool(cfg.get("enabled"))),
            })
            out.append(item)
        return out

    def set_loop_params(self, loop_id: str, params: dict) -> dict:
        """更新某回路的运行参数（目标/Kp/Ki/Kd/启用）——写回卡片配置。"""
        with self._lock:
            loop = next((l for l in self._loops if l["id"] == loop_id), None)
        if not loop:
            raise KeyError(loop_id)
        try:
            layout = store.get_json("dashboard_layout", None) or {}
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"布局读取失败: {exc}") from exc
        widgets = layout.get("widgets") or []
        for w in widgets:
            if isinstance(w, dict) and str(w.get("id")) == loop_id:
                cfg = w.get("pid") if isinstance(w.get("pid"), dict) else {}
                cfg.update({k: v for k, v in params.items() if v is not None})
                w["pid"] = cfg
                break
        else:
            raise KeyError(loop_id)
        store.set_json("dashboard_layout", layout)
        self.reload()
        return next((l for l in self.loops() if l["id"] == loop_id), {})

    # ---------- 运行 ----------
    def tick(self, data: dict) -> None:
        """采集循环每周期调用：没有闭环卡时走默认回路（兼容旧行为）。"""
        with self._lock:
            loops = list(self._loops)
        if not loops:
            self._tick_default(data)
            return
        for loop in loops:
            try:
                self._run_loop(loop, data)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    loop["error"] = str(exc)[:200]

    def _run_loop(self, loop: dict, data: dict) -> None:
        cfg = loop["cfg"] or {}
        # 老式闭环卡（没有在卡片配置里选来源卡/执行器卡）= 旧版默认回路：
        # 加热槽温度 → 本机加热，启用与参数沿用 sys.pid_*（保持既有布局行为不变）。
        legacy = not cfg.get("sensor_card") and not cfg.get("actuator_card")
        enabled = cfg.get("enabled")
        if enabled is None:
            enabled = bool(store.pid_enabled()) if legacy else True
        if not enabled:
            return
        if legacy:
            source = {"kind": "realtime", "path": "heater_temp"}
        else:
            source = self._source_of(cfg.get("sensor_card"), cfg.get("sensor_source"))
        measured = self._read(source, cfg.get("sensor_card"), data)
        if measured is None:
            with self._lock:
                loop["measured"] = None
                loop["error"] = "温度无有效读数，本轮跳过"
            return
        # 前置条件（防干烧联锁）：不满足则强制断开加热
        guard_ok, guard_text = self._guard(cfg, data) if cfg.get("guard_card") else (True, "")
        if not guard_ok:
            self._apply(loop, cfg, "off")
            with self._lock:
                loop["measured"] = measured
                loop["guard_ok"] = False
                loop["error"] = guard_text
            return
        target = float(cfg.get("target") if cfg.get("target") is not None else store.target_temp())
        pid = self._pid_for(loop["id"], cfg)
        duty = pid.update(target, measured, store.period())
        act = heater_action(duty)
        self._apply(loop, cfg, act, target=target, duty=duty, legacy=legacy)
        with self._lock:
            loop["measured"] = measured
            loop["duty"] = duty
            loop["guard_ok"] = True
            loop["error"] = ""

    # ---------- 取值与执行 ----------
    def _source_of(self, card_id, fallback):
        """取值来源：卡片当前配置优先，卡片被删时用保存时快照。"""
        with self._lock:
            card = self._cards.get(str(card_id or ""))
        src = (card or {}).get("source")
        return src if isinstance(src, dict) else (fallback or {})

    def _read(self, source: dict, card_id, data: dict):
        kind = (source or {}).get("kind")
        if kind == "realtime":
            return _numeric(_get_path(data, source.get("path", "")))
        if kind == "custom":
            return _numeric((data.get("custom") or {}).get(str(card_id or "")))
        return None

    def _guard(self, cfg: dict, data: dict):
        """前置条件（可选）：guard_card 的读数必须 ≥ guard_min，否则禁止加热。"""
        card_id = cfg.get("guard_card")
        if not card_id:
            return True, ""
        source = self._source_of(card_id, cfg.get("guard_source"))
        value = self._read(source, card_id, data)
        try:
            low = float(cfg.get("guard_min") if cfg.get("guard_min") is not None else 0)
        except (TypeError, ValueError):
            low = 0.0
        if value is None:
            return False, "前置条件卡无有效读数，禁止加热（防干烧）"
        if value < low:
            return False, f"前置条件未满足（{value:.1f} < {low:g}），强制断开加热"
        return True, ""

    def _pid_for(self, loop_id: str, cfg: dict) -> PID:
        def param(key, default):
            try:
                return float(cfg.get(key) if cfg.get(key) is not None else default)
            except (TypeError, ValueError):
                return float(default)
        with self._lock:
            pid = self._pids.get(loop_id)
            if pid is None:
                pid = PID(param("kp", store.pid_kp()), param("ki", store.pid_ki()), param("kd", store.pid_kd()))
                self._pids[loop_id] = pid
            else:
                pid.kp = param("kp", store.pid_kp())
                pid.ki = param("ki", store.pid_ki())
                pid.kd = param("kd", store.pid_kd())
            return pid

    def _resolve_actuator(self, cfg: dict) -> dict:
        """执行器解析：卡片当前配置优先，卡片被删时用保存时快照。"""
        out = dict(cfg.get("actuator") or {})
        with self._lock:
            card = self._cards.get(str(cfg.get("actuator_card") or ""))
        if not isinstance(card, dict):
            return out
        out["title"] = card.get("title") or ""
        cmd = card.get("cmd") or {}
        if cmd.get("on") or cmd.get("off"):
            out["kind"] = "url"
            out["url_on"], out["url_off"] = cmd.get("on") or "", cmd.get("off") or ""
        elif card.get("ctl") == "heater":
            out["kind"] = "heater"
        elif card.get("ctl") == "pump":
            out["kind"] = "pump"
        return out

    def _apply(self, loop: dict, cfg: dict, action: str, target: float | None = None,
               duty: float | None = None, legacy: bool = False) -> None:
        """下发加热开/关：状态未变化则不重复下发（去抖），并写操作日志。"""
        with self._lock:
            prev = self._last_action.get(loop["id"])
        target = float(target if target is not None else (cfg.get("target") or store.target_temp()))
        duty = float(duty if duty is not None else 0.0)
        spec = self._resolve_actuator(cfg) if not legacy else {"kind": "heater"}
        kind = spec.get("kind") or "heater"
        label = spec.get("title") or loop["name"]
        if action == "on":
            url = spec.get("url_on") or ""
        else:
            url = spec.get("url_off") or ""
        if kind == "url" and not url:
            with self._lock:
                loop["error"] = f"执行器[{label}] 未配置该方向的指令 URL"
            return
        if kind == "url":
            # 自定义指令没有状态回读：按本回路上次下发过的状态去抖，避免每周期重复发指令
            with self._lock:
                last_effective = self._last_action.get(loop["id"], "")
        else:
            last_effective = self._effective_state(kind, spec)
        if action == last_effective:
            with self._lock:
                loop["action"] = action
            return
        detail = (f"恒温闭环[{loop['name']}] 自动{'开启' if action == 'on' else '关闭'}"
                  f"(目标{target:g}℃,占空比{duty:.0f}%) → 执行器[{label}]")
        try:
            if kind == "url":
                self._http_get(url)
                log_action = f"pid_actuator_{action}"
            elif kind == "pump":
                self.plant.pump_control(action)
                log_action = f"pid_pump_{action}"
            else:
                self.plant.heater_control(action)
                log_action = f"heater_{action}"
            database.insert_control_log(log_action, "success", detail, operator=None, source="auto")
            with self._lock:
                self._last_action[loop["id"]] = action
                loop["action"] = action
                loop["error"] = ""
        except Exception as exc:  # noqa: BLE001
            database.insert_control_log("pid_fail", "fail", f"{detail} 失败: {exc}"[:250],
                                        operator=None, source="auto")
            with self._lock:
                loop["error"] = str(exc)[:200]

    def _effective_state(self, kind: str, spec: dict) -> str:
        """内置执行器当前状态：以设备回读为准（plant.heater_state / pump_state，取值 on/off/unknown）"""
        if self.plant is None:
            return ""
        return self.plant.heater_state if kind == "heater" else self.plant.pump_state

    @staticmethod
    def _http_get(url: str) -> None:
        """下发自定义指令（仅 GET，主机须在代理白名单内）。"""
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"非法指令 URL: {url}")
        if u.hostname not in config.DASHBOARD_PROXY_HOSTS:
            raise ValueError(f"指令目标主机不在白名单: {u.hostname}")
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=config.DEVICE_TIMEOUT_S) as resp:
            resp.read(4096)

    # ---------- 默认回路（无闭环卡时的旧行为） ----------
    def _tick_default(self, data: dict) -> None:
        """加热槽温度 → 本机加热（与旧版单回路一致，参数取 sys.pid_*）。"""
        if not config.pid_supported() or not store.pid_enabled():
            return
        measured = _numeric(data.get("heater_temp"))
        if measured is None or self.plant is None:
            return
        target = store.target_temp()
        pid = self._pid_for("__default__", {})
        duty = pid.update(target, measured, store.period())
        action = heater_action(duty)
        if action == self.plant.heater_state:
            return
        self.plant.heater_control(action)
        name = "开启加热" if action == "on" else "关闭加热"
        database.insert_control_log(f"heater_{action}", "success",
                                    f"恒温闭环自动{name}(目标{target:g}℃,占空比{duty:.0f}%)",
                                    operator=None, source="auto")
