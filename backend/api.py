"""HTTP 业务接口（FastAPI 路由）：水循环实时/历史/控制/统计/告警/PID/判定 + 账号鉴权。

统一返回 {"code":0,"msg":"ok","data":...}；前端通过 /api/* 调用，
接口文档启动后访问 /docs 自动生成。
"""
import json
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

import auth
import config
import database
import store
from state import services

router = APIRouter(prefix="/api")


def ok(data=None) -> dict:
    return {"code": 0, "msg": "ok", "data": data}


def err(code: int, msg: str, data=None) -> dict:
    return {"code": code, "msg": msg, "data": data}


class ActionRequest(BaseModel):
    # toggle 由设备固件执行；前端默认使用确定的 on/off
    action: str = Field(pattern="^(on|off|toggle)$")


class PumpTargetRequest(BaseModel):
    """定量浇水目标(L)：累计水量达到该值后由设备自动关泵。0 表示取消定量。"""
    liters: float = Field(ge=0, le=10000)


class TankRequest(BaseModel):
    """水槽容积(L)：用于把液位百分比换算成界面上的估算水量。"""
    tank: str = Field(default="storage", pattern="^(storage|heater)$")
    capacity: float = Field(gt=0, le=1_000_000)


class TargetRequest(BaseModel):
    temp: float = Field(ge=0, le=90)


class PidModeRequest(BaseModel):
    enabled: int = Field(ge=0, le=1)


class PidParamsRequest(BaseModel):
    kp: float | None = Field(default=None, ge=0, le=500)
    ki: float | None = Field(default=None, ge=0, le=100)
    kd: float | None = Field(default=None, ge=0, le=500)


class PeriodRequest(BaseModel):
    period: float = Field(ge=0.5, le=10)


class AlarmConfigRequest(BaseModel):
    """告警阈值：仅当前设备支持的通道（流量/温度/压力/光照）。压力单位 kPa，光照单位 lx。"""
    flow_max: float | None = Field(default=None, ge=0, le=100)
    flow_min: float | None = Field(default=None, ge=0, le=100)
    storage_temp_max: float | None = Field(default=None, ge=0, le=100)
    storage_temp_min: float | None = Field(default=None, ge=-20, le=100)
    heater_temp_max: float | None = Field(default=None, ge=0, le=100)
    heater_temp_min: float | None = Field(default=None, ge=-20, le=100)
    pressure_max: float | None = Field(default=None, ge=0, le=2000)
    pressure_min: float | None = Field(default=None, ge=0, le=2000)
    light_max: float | None = Field(default=None, ge=0, le=200000)
    light_min: float | None = Field(default=None, ge=0, le=200000)


class IngestRequest(BaseModel):
    """采集端主动上报(可选链路)：仅设备实际提供的字段。"""
    flow_rate: float | None = None
    total_liters: float | None = None
    storage_temp: float | None = None
    heater_temp: float | None = None
    pressure: float | None = None
    light: float | None = None


def _validate_range(start: str | None, end: str | None) -> None:
    if not start or not end:
        raise ValueError("缺少 start/end 参数")
    for value in (start, end):
        try:
            datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise ValueError(f"时间格式错误: {value}") from exc
    if start > end:
        raise ValueError("start 不能晚于 end")


def _unsupported(feature: str, label: str) -> dict:
    """设备能力缺失时的统一错误返回。"""
    return err(40003, f"当前采集设备不支持{label}（固件未提供该通道，见 config.DEVICE_FEATURES）")


def _tank_info(tank: str, data: dict) -> dict:
    """单个水槽的水位信息。

    液位来自该水槽的独立液位通道：
      已接入(DEVICE_FEATURES["level_<tank>"]=True) → 真实读数，source="device"；
      未接入                                        → 恒为 0，      source="none"（界面标注「无传感器」）。
    无数据一律按 0 展示，不使用任何模拟值（用户要求：清除模拟数据，没有数据就保持 0）。
    "估算水量" = 液位% × 该水槽容积，仅作展示参考，不是计量值。
    """
    available = bool(config.DEVICE_FEATURES.get(f"level_{tank}"))
    percent = data.get(f"level_{tank}") if available else None
    if percent is None:
        percent = 0.0
    percent = max(0.0, min(100.0, float(percent)))
    capacity = store.tank_capacity(tank)
    height = (config.TANK_HEIGHT_CM_STORAGE if tank == "storage"
              else config.TANK_HEIGHT_CM_HEATER)
    return {
        "key": tank,
        "label": config.TANK_LABELS.get(tank, tank),
        "percent": round(percent, 2),
        "source": "device" if available else "none",
        "capacity": round(capacity, 3),
        "volume": round(capacity * percent / 100.0, 3),   # 估算水量(L)，非计量值
        "height_cm": round(height * percent / 100.0, 1),
    }


def _level_info() -> dict:
    """双水槽水位信息（供前端 SVG 动画使用）。

    any_unavailable：存在未接入传感器的槽位（该槽水位恒为 0，界面标注「无传感器」）。
    设备是否在线由 snapshot 的 sensor_online 单独给出，前端据此显示「离线」。
    """
    plant = services.plant
    data = plant.status() if plant else {}
    tanks = {tank: _tank_info(tank, data) for tank in config.TANKS}
    return {
        "tanks": tanks,
        "any_unavailable": any(t["source"] != "device" for t in tanks.values()),
    }


def _snapshot() -> dict:
    """实时快照：设备最新读数 + 链路状态 + 设备能力 + 双水槽水位 + 活跃告警。

    读的是设备模型缓存(由 1Hz 采集循环刷新)，不额外发起 HTTP 请求，
    避免前端轮询放大对 ESP32 的压力。
    """
    plant = services.plant
    data = plant.status() if plant else {}
    alarms = services.alarm.active_alarms() if services.alarm else []
    return {
        # 真实通道
        "flow_rate": data.get("flow_rate"),
        "total_liters": data.get("total_liters"),
        "total_flow": data.get("total_liters"),   # 兼容旧字段名：累计水量(L)
        "pump_state": data.get("pump_state"),
        "pump_target": data.get("pump_target"),
        "target_baseline": store.target_baseline(),   # 设定定量目标时的累计水量(L)，用于进度换算
        "pulses": data.get("pulses"),
        "window_ms": data.get("window_ms"),
        # 液位（双水槽；仅加热槽为超声波实测，储水槽未接入传感器 → 水位恒为 0）
        "level_storage": data.get("level_storage"),
        "level_heater": data.get("level_heater"),
        "tank": _level_info(),
        # 温度/压力/加热/光照通道（离线或读数无效时为 None）
        "temperature": data.get("temperature"),
        "storage_temp": data.get("storage_temp"),
        "heater_temp": data.get("heater_temp"),
        "pressure": data.get("pressure"),
        "heater_state": data.get("heater_state"),
        "light": data.get("light"),
        # 链路与设备信息
        "sensor_online": data.get("sensor_online"),
        "last_error": data.get("last_error") or "",
        "device": data.get("device") or {},
        "features": data.get("features") or dict(config.DEVICE_FEATURES),
        "pid_supported": config.pid_supported(),
        # 运行信息
        "period": store.period(),
        "active_alarms": alarms,
        "alarm_count": len(alarms),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------- 实时数据 ----------
@router.get("/water/realtime")
def realtime(_: dict = Depends(auth.require_perm("view_monitor"))):
    return ok(_snapshot())


# ---------- 历史数据 ----------
@router.get("/water/history")
def history(
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    limit: int = Query(default=5000, ge=1, le=50000),
    _: dict = Depends(auth.require_perm("view_history")),
):
    try:
        _validate_range(start, end)
    except ValueError as exc:
        return err(40002, str(exc))
    return ok({"points": database.query_water_history(start, end, limit)})


# ---------- 执行器控制 ----------
def _control(device: str, action: str, label: str, user: dict):
    """下发执行器控制指令，返回最新快照；失败返回 40003。操作账号写入日志。"""
    if services.plant is None:
        return err(40004, "设备未初始化")
    who = user.get("username") or None
    try:
        if device == "pump":
            services.plant.pump_control(action)
        else:
            services.plant.heater_control(action)
    except Exception as exc:  # noqa: BLE001  (含 DeviceError：网络失败/设备不支持)
        database.insert_control_log(f"{device}_{action}", "failed", f"控制失败: {exc}",
                                    operator=who, source="manual")
        return err(40003, f"控制指令执行失败: {exc}")
    action_text = {"on": "开启", "off": "关闭", "toggle": "切换"}.get(action, action)
    database.insert_control_log(f"{device}_{action}", "success", f"手动控制 {label} {action_text}",
                                operator=who, source="manual")
    return ok(_snapshot())


@router.post("/water/pump")
def pump(body: ActionRequest, user: dict = Depends(auth.require_perm("ctrl_light"))):
    return _control("pump", body.action, "水泵", user)


@router.post("/water/heater")
def heater(body: ActionRequest, user: dict = Depends(auth.require_perm("ctrl_light"))):
    if not config.DEVICE_FEATURES.get("heater"):
        return _unsupported("heater", "加热模块")
    return _control("heater", body.action, "加热模块", user)


# ---------- 定量浇水（设备固件侧自动关泵） ----------
@router.get("/water/pump/target")
def pump_target_get(_: dict = Depends(auth.require_perm("view_monitor"))):
    if not config.DEVICE_FEATURES.get("pump_target"):
        return _unsupported("pump_target", "定量目标")
    try:
        target = services.plant.client.get_pump_target()
    except Exception as exc:  # noqa: BLE001
        return err(40003, f"读取定量目标失败: {exc}")
    return ok({"target": target})


@router.post("/water/pump/target")
def pump_target_set(body: PumpTargetRequest, user: dict = Depends(auth.require_perm("ctrl_light"))):
    if not config.DEVICE_FEATURES.get("pump_target"):
        return _unsupported("pump_target", "定量目标")
    who = user.get("username") or None
    try:
        target = services.plant.set_pump_target(body.liters)
    except Exception as exc:  # noqa: BLE001
        database.insert_control_log("pump_target", "failed", f"设定定量目标失败: {exc}",
                                    operator=who, source="manual")
        return err(40003, f"设定定量目标失败: {exc}")
    # 记录本次定量的起始累计水量，供界面计算"本次已注入量"进度
    if target > 0:
        store.set_target_baseline(services.plant.status().get("total_liters") or 0.0)
    else:
        store.set_target_baseline(None)
    detail = f"设定定量浇水目标 {target} L（达到后自动关泵）" if target > 0 else "取消定量浇水目标"
    database.insert_control_log("pump_target", "success", detail, operator=who, source="manual")
    snap = _snapshot()
    snap["target"] = target
    return ok(snap)


# ---------- 双水槽容积（用于估算水量） ----------
@router.post("/water/tank")
def tank_set(body: TankRequest, user: dict = Depends(auth.require_perm("cfg_system"))):
    """设置指定水槽的容积(L)。"""
    store.set(f"tank_capacity_{body.tank}", body.capacity)
    label = config.TANK_LABELS.get(body.tank, body.tank)
    database.insert_control_log(
        "config.tank", "success", f"设置{label}容积 = {body.capacity} L",
        operator=user.get("username") or None, source="manual")
    return ok({
        "tank": body.tank,
        "tank_capacity": store.tank_capacity(body.tank),
        "capacities": {t: store.tank_capacity(t) for t in config.TANKS},
        "tanks": _level_info()["tanks"],
    })


# ---------- 累计水量清零（破坏性操作，前端二次确认） ----------
@router.post("/water/volume/reset")
def volume_reset(user: dict = Depends(auth.require_perm("ctrl_light"))):
    if not config.DEVICE_FEATURES.get("volume_reset"):
        return _unsupported("volume_reset", "累计水量清零")
    who = user.get("username") or None
    try:
        total = services.plant.reset_volume()
    except Exception as exc:  # noqa: BLE001
        database.insert_control_log("volume_reset", "failed", f"清零累计水量失败: {exc}",
                                    operator=who, source="manual")
        return err(40003, f"清零累计水量失败: {exc}")
    database.insert_control_log("volume_reset", "success", "清零累计水量",
                                operator=who, source="manual")
    snap = _snapshot()
    snap["totalLiters"] = total
    return ok(snap)


# ---------- 设备信息（任务一/二：链路状态） ----------
@router.get("/water/device")
def device_info(_: dict = Depends(auth.require_perm("view_device"))):
    """设备链路信息：地址、在线状态、IP/RSSI/运行时长、支持通道。"""
    plant = services.plant
    if plant is None:
        return err(40004, "设备未初始化")
    info = plant.status()
    device = dict(info.get("device") or {})
    uptime_ms = device.get("uptime_ms")
    device["uptime_s"] = round(uptime_ms / 1000.0, 1) if isinstance(uptime_ms, (int, float)) else None
    device["last_error"] = info.get("last_error") or ""
    return ok({"online": bool(info.get("sensor_online")), "device": device,
               "features": info.get("features") or {}})


# ---------- 采集端上报(任务二：底层→后端 HTTP 传输，可选链路) ----------
@router.post("/water/ingest")
def ingest(body: IngestRequest, _: dict = Depends(auth.require_perm("view_monitor"))):
    """采集端(ESP32/上位机)每周期把数据 POST 到此接口。

    后端默认主动轮询设备(见 water_device)，本接口用于设备主动推送的场景，
    收到后 2 秒内的上报值会覆盖轮询值。
    """
    services.ingest = {**body.model_dump(exclude_none=True), "ts": datetime.now().timestamp()}
    return ok({"msg": "已接收采集端数据"})


# ---------- 恒温PID(任务六) ----------
# 恒温闭环需设备同时提供温度采集与加热控制；两项能力均在 config.DEVICE_FEATURES 声明，
# 任一缺失时以下接口统一返回不可用（40003）。
@router.post("/water/target")
def set_target(body: TargetRequest, _: dict = Depends(auth.require_perm("cfg_alarm"))):
    if not config.pid_supported():
        return _unsupported("temperature/heater", "恒温闭环控制")
    store.set("target_temp", body.temp)
    if services.pid:
        services.pid.reset()
    return ok({"target_temp": store.target_temp()})


@router.post("/water/pid/mode")
def pid_mode(body: PidModeRequest, _: dict = Depends(auth.require_perm("cfg_alarm"))):
    if not config.pid_supported():
        return _unsupported("temperature/heater", "恒温闭环控制")
    store.set("pid_enabled", body.enabled)
    if services.pid:
        services.pid.reset()
    return ok({"pid_enabled": bool(store.pid_enabled())})


@router.get("/water/pid")
def pid_get(_: dict = Depends(auth.require_perm("view_history"))):
    return ok({
        "supported": config.pid_supported(),
        "target_temp": store.target_temp(),
        "pid_enabled": bool(store.pid_enabled()),
        "kp": store.pid_kp(), "ki": store.pid_ki(), "kd": store.pid_kd(),
    })


@router.post("/water/pid")
def pid_set(body: PidParamsRequest, _: dict = Depends(auth.require_perm("cfg_alarm"))):
    if not config.pid_supported():
        return _unsupported("temperature/heater", "恒温闭环控制")
    updates = body.model_dump(exclude_none=True)
    for key, value in updates.items():
        store.set(f"pid_{key}", value)
    if services.pid:
        services.pid.kp = store.pid_kp()
        services.pid.ki = store.pid_ki()
        services.pid.kd = store.pid_kd()
        services.pid.reset()
    return ok({"msg": "PID参数已保存"})


# ---------- 采集周期 ----------
@router.post("/water/period")
def period_set(body: PeriodRequest, user: dict = Depends(auth.require_perm("cfg_system"))):
    old = store.period()
    store.set("period", body.period)
    database.insert_control_log("config.period", "success",
                                f"采集周期 {old} → {store.period()} 秒",
                                operator=user.get("username") or None, source="manual")
    return ok({"period": store.period()})


# ---------- 统计面板(任务六) ----------
@router.get("/water/stats")
def stats(
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    _: dict = Depends(auth.require_perm("view_history")),
):
    try:
        _validate_range(start, end)
    except ValueError as exc:
        return err(40002, str(exc))
    return ok(database.query_water_stats(start, end))


# ---------- 告警 ----------
@router.get("/water/alarm/config")
def alarm_config_get(_: dict = Depends(auth.require_perm("view_alarm"))):
    thresholds = services.alarm.thresholds if services.alarm else {}
    return ok({"thresholds": thresholds, "active": services.alarm.active_alarms() if services.alarm else []})


@router.post("/water/alarm/config")
def alarm_config_set(body: AlarmConfigRequest, user: dict = Depends(auth.require_perm("cfg_alarm"))):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return err(40002, "未提供任何阈值")
    before = services.alarm.thresholds if services.alarm else {}
    for key, value in updates.items():
        database.set_config(key, str(value))
    if services.alarm:
        services.alarm.reload()
    changed = ", ".join(
        f"{k}: {before.get(k, '--')} → {v}" for k, v in updates.items()
    )
    # detail 列为 VARCHAR(255)：阈值字段多时整包可能超长，截断保护
    detail = f"修改告警阈值 {changed}"
    if len(detail) > 250:
        detail = detail[:250] + "…"
    database.insert_control_log("config.alarm", "success", detail,
                                operator=user.get("username") or None, source="manual")
    return ok({"thresholds": services.alarm.thresholds})


@router.get("/water/alarms")
def alarms(
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=200),
    _: dict = Depends(auth.require_perm("view_alarm")),
):
    items, total = database.query_alarms(start, end, type, status, page, page_size)
    return ok({"items": items, "total": total, "page": page, "page_size": page_size})


@router.get("/water/alarm/stats")
def alarm_stats(_: dict = Depends(auth.require_perm("view_alarm"))):
    return ok({"stats": database.query_alarm_stats()})


# ---------- 操作日志 ----------
@router.get("/water/logs")
def logs(
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    category: str | None = Query(default=None, pattern="^(device|config|account)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=200),
    _: dict = Depends(auth.require_perm("view_log")),
):
    items, total = database.query_control_log(start, end, page, page_size, category)
    return ok({"items": items, "total": total, "page": page, "page_size": page_size})


# ---------- 判定服务(任务五) ----------
@router.get("/water/judge/status")
def judge_status(_: dict = Depends(auth.require_perm("view_device"))):
    return ok(services.judge.status() if services.judge else {})


@router.post("/water/judge/enable")
def judge_enable(body: dict, _: dict = Depends(auth.require_perm("cfg_system"))):
    store.set("judge_enabled", 1 if bool(body.get("enabled")) else 0)
    return ok({"enabled": bool(store.judge_enabled())})


# ---------- 系统状态 ----------
@router.get("/water/system")
def system_status(_: dict = Depends(auth.require_perm("view_device"))):
    plant_info = services.plant.status() if services.plant else {}
    return ok({
        "uptime_s": round(services.uptime, 1),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "period": store.period(),
        "database_ok": database.ping(),
        "device_url": config.DEVICE_URL,
        "device_online": bool(plant_info.get("sensor_online")),
        "tank_capacities": {t: store.tank_capacity(t) for t in config.TANKS},
        "features": dict(config.DEVICE_FEATURES),
        "pid_supported": config.pid_supported(),
        "active_alarm_count": len(services.alarm.active_alarms()) if services.alarm else 0,
    })


# =============================================================
# 仪表盘布局（全局共享一套，存 config 表 sys.dashboard_layout）
# 前端卡片化仪表盘：布局/卡片配置整体为一份 JSON，由前端解释结构，
# 后端只负责持久化、权限与审计；自定义卡片的数据请求走 proxy 白名单代发。
# =============================================================
class DashboardLayoutRequest(BaseModel):
    """仪表盘布局配置：{widgets: [...]}，单卡片字段由前端 widgets.js 定义。"""
    layout: dict


@router.get("/water/dashboard/layout")
def dashboard_layout_get(_: dict = Depends(auth.require_perm("view_monitor"))):
    """读取全局仪表盘布局；从未保存过返回 layout=null（前端用内置默认布局）。"""
    return ok({"layout": store.get_json("dashboard_layout", None)})


@router.post("/water/dashboard/layout")
def dashboard_layout_set(body: DashboardLayoutRequest,
                         user: dict = Depends(auth.require_perm("cfg_system"))):
    """保存全局仪表盘布局（覆盖式）。"""
    raw = json.dumps(body.layout, ensure_ascii=False)
    if len(raw) > 200_000:
        return err(40002, "布局配置过大（超过 200KB）")
    store.set_json("dashboard_layout", body.layout)
    database.insert_control_log(
        "config.dashboard", "success",
        f"更新仪表盘布局（{len(body.layout.get('widgets', []))} 张卡片）",
        operator=user.get("username") or None, source="manual")
    return ok({"layout": body.layout})


@router.post("/water/dashboard/layout/reset")
def dashboard_layout_reset(user: dict = Depends(auth.require_perm("cfg_system"))):
    """清除已保存布局，前端回退为内置默认布局。"""
    store.set("dashboard_layout", "")
    database.insert_control_log("config.dashboard", "success", "恢复默认仪表盘布局",
                                operator=user.get("username") or None, source="manual")
    return ok({"layout": None})


@router.get("/water/dashboard/proxy")
def dashboard_proxy(url: str = Query(..., max_length=1000),
                    log: int = Query(0),
                    user: dict = Depends(auth.require_perm("view_monitor"))):
    """自定义卡片数据源/控制指令代发（仅 GET）：仅允许白名单主机，防 SSRF 与跨域问题。

    白名单见 config.DASHBOARD_PROXY_HOSTS（默认本机 + 现场采集设备）。
    目标返回 JSON 时取 json 字段；非 JSON（如部分指令接口）返回 text 字段，均不算失败。
    log=1 时视为一次控制指令下发，写入操作日志（device.custom）供审计。
    """
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return err(40002, "非法 URL（仅支持 http/https）")
    if u.hostname not in config.DASHBOARD_PROXY_HOSTS:
        return err(40002, f"目标主机不在白名单: {u.hostname}")
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=config.DEVICE_TIMEOUT_S) as resp:
            body = resp.read(131_072)
        text = body.decode("utf-8", "replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if log:
            database.insert_control_log("device.custom", "success",
                                        f"自定义指令: {url[:250]}",
                                        operator=user.get("username") or None, source="manual")
        return ok({"json": payload, "text": None if payload is not None else text[:2000]})
    except Exception as exc:  # noqa: BLE001
        if log:
            database.insert_control_log("device.custom", "fail",
                                        f"自定义指令失败: {url[:200]} · {exc}"[:250],
                                        operator=user.get("username") or None, source="manual")
        return err(40003, f"请求失败: {exc}")


# =============================================================
# 账号与权限
# =============================================================
def _user_public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "password_hash"}


@router.post("/auth/login")
def login(body: dict):
    username = str(body.get("username", ""))
    password = str(body.get("password", ""))
    row = database.get_user(username)
    if row is None or not auth.verify_password(password, row["password_hash"]):
        return err(40006, "用户名或密码错误")
    if row["status"] != "active":
        return err(40006, "账号已被禁用")
    token = auth.make_token(row["username"], row["role"])
    perms = auth.perms_of(row["role"])
    return ok({"token": token, "user": {"username": row["username"], "role": row["role"], "perms": perms}})


@router.get("/auth/me")
def auth_me(user: dict = Depends(auth.get_current_user)):
    return ok({"username": user["username"], "role": user["role"],
               "perms": user["perms"], "role_label": auth.role_label(user["role"])})


@router.post("/auth/change_password")
def change_password(body: dict, user: dict = Depends(auth.get_current_user)):
    row = database.get_user(user["username"])
    if row is None:
        return err(40004, "用户不存在")
    database.update_user_password(row["id"], auth.hash_password(body.get("password", "")))
    return ok({"msg": "密码已修改"})


@router.get("/auth/users")
def auth_users(_: dict = Depends(auth.require_perm("account_manage"))):
    return ok({"users": [_user_public(r) for r in database.query_users()]})


def _log_account(action: str, detail: str, user: dict, result: str = "success") -> None:
    """账号管理类操作统一入日志（不记录任何密码明文）。"""
    database.insert_control_log(action, result, detail,
                                operator=user.get("username") or None, source="manual")


@router.post("/auth/users")
def auth_create_user(body: dict, user: dict = Depends(auth.require_perm("account_manage"))):
    username = str(body.get("username", "")).strip()
    if not username:
        return err(40002, "用户名不能为空")
    if database.get_user(username) is not None:
        return err(40002, "用户名已存在")
    role = str(body.get("role", "viewer"))
    if role not in auth.role_matrix():
        return err(40002, f"角色不存在: {role}")
    user_id = database.insert_user(username, auth.hash_password(str(body.get("password", ""))), role)
    _log_account("account.create", f"创建账号 {username}（角色 {auth.role_label(role)}）", user)
    return ok({"id": user_id, "msg": f"已创建用户 {username}"})


@router.post("/auth/users/{user_id}/password")
def auth_reset_password(user_id: int, body: dict, user: dict = Depends(auth.require_perm("account_manage"))):
    target = next((r for r in database.query_users() if r["id"] == user_id), None)
    database.update_user_password(user_id, auth.hash_password(str(body.get("password", ""))))
    name = target["username"] if target else f"#{user_id}"
    _log_account("account.password", f"重置账号 {name} 的密码", user)
    return ok({"msg": "密码已重置"})


@router.post("/auth/users/{user_id}/role")
def auth_set_role(user_id: int, body: dict, user: dict = Depends(auth.require_perm("account_manage"))):
    if body.get("role") not in auth.role_matrix():
        return err(40002, f"角色不存在: {body.get('role')}")
    target = next((r for r in database.query_users() if r["id"] == user_id), None)
    old_role = target["role"] if target else "--"
    database.update_user_role(user_id, body["role"])
    name = target["username"] if target else f"#{user_id}"
    _log_account("account.role",
                 f"账号 {name} 角色 {auth.role_label(old_role)} → {auth.role_label(body['role'])}", user)
    return ok({"msg": "角色已更新"})


@router.post("/auth/users/{user_id}/status")
def auth_set_status(user_id: int, body: dict, user: dict = Depends(auth.require_perm("account_manage"))):
    target = next((r for r in database.query_users() if r["id"] == user_id), None)
    if target is None:
        return err(40004, "用户不存在")
    if target["username"] == user["username"]:
        return err(40002, "不能禁用自己")
    if target["username"] == config.ADMIN_USERNAME:
        return err(40002, "不能禁用管理员账号")
    database.update_user_status(user_id, body["status"])
    text = "启用" if body["status"] == "active" else "禁用"
    _log_account("account.status", f"{text}账号 {target['username']}", user)
    return ok({"msg": "状态已更新"})


@router.delete("/auth/users/{user_id}")
def auth_delete_user(user_id: int, user: dict = Depends(auth.require_perm("account_manage"))):
    target = next((r for r in database.query_users() if r["id"] == user_id), None)
    if target is None:
        return err(40004, "用户不存在")
    if target["username"] == user["username"]:
        return err(40002, "不能删除自己")
    if target["username"] == config.ADMIN_USERNAME:
        return err(40002, "不能删除管理员账号")
    database.delete_user(user_id)
    _log_account("account.delete", f"删除账号 {target['username']}（角色 {auth.role_label(target['role'])}）", user)
    return ok({"msg": "用户已删除"})


@router.get("/auth/roles")
def auth_roles(_: dict = Depends(auth.require_perm("account_manage"))):
    return ok({"permissions": auth.PERMISSIONS, "roles": auth.role_matrix()})


@router.post("/auth/roles")
def auth_save_roles(body: dict, user: dict = Depends(auth.require_perm("account_manage"))):
    matrix = {}
    for key, val in body.get("roles", {}).items():
        if not str(key).strip():
            continue
        if isinstance(val, (list, tuple)):
            # 形式1: [显示名, 权限列表] 或 [显示名, "*"]
            if val and len(val) >= 2 and isinstance(val[0], str):
                label, perms = val[0], val[1]
                if perms == "*":
                    matrix[str(key)] = [label, "*"]
                elif isinstance(perms, (list, tuple)):
                    matrix[str(key)] = [label, [str(p) for p in perms]]
                else:  # 兜底：单权限
                    matrix[str(key)] = [label, [str(perms)]]
            # 形式2: 旧格式纯权限列表 [p1, p2, ...]
            elif val:
                matrix[str(key)] = [str(v) for v in val]
        elif isinstance(val, dict):
            label = val.get("label", key)
            plist = val.get("perms")
            matrix[str(key)] = [label, "*" if plist == "*" else [str(p) for p in (plist or [])]]
    matrix.setdefault("admin", auth.DEFAULT_ROLES["admin"])
    if matrix.get("admin") != auth.DEFAULT_ROLES["admin"]:
        matrix["admin"] = auth.DEFAULT_ROLES["admin"]
    auth.save_role_matrix(matrix)
    _log_account("account.roles", f"保存角色权限矩阵（{len(matrix)} 个角色）", user)
    return ok({"msg": "角色矩阵已保存", "roles": auth.role_matrix()})