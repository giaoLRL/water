"""HTTP 业务接口（FastAPI 路由）：水循环实时/历史/控制/统计/告警/PID/判定 + 账号鉴权。

统一返回 {"code":0,"msg":"ok","data":...}；前端通过 /api/* 调用，
接口文档启动后访问 /docs 自动生成。
"""
import csv
import io
import json
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

import auth
import calibration
import config
import database
import judge_service
import store
import water_device
from alarm import LINK_DIRECTIONS, LINK_SOURCE_KINDS, LINK_TARGET_KINDS
from gateway import FUNCS, MODES, MODE_RTU_TCP, MODE_TCP, REG_TYPES
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


def _custom_values() -> dict:
    """自定义通道有效值：卡片声明的通道 + 外部设备（Modbus/串口）映射的 custom:xxx 合并。"""
    values = dict(services.custom.values()) if services.custom else {}
    if services.gateway:
        for key, value in services.gateway.values().items():
            if key.startswith("custom:"):
                values[key.split(":", 1)[1]] = value
    return values


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


def _level_info() -> dict:
    """双水槽水位信息（供前端 SVG 动画使用）。

    any_unavailable：存在未接入传感器的槽位（该槽水位恒为 0，界面标注「无传感器」）。
    设备是否在线由 snapshot 的 sensor_online 单独给出，前端据此显示「离线」。
    逻辑与采集循环共用 water_device.tank_snapshot（单一来源，避免两处口径不一致）。
    """
    plant = services.plant
    data = plant.status() if plant else {}
    return water_device.tank_snapshot(data)


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
        # 自定义传感器通道（全链路：卡片声明+最近读数，卡片取值路径 custom.<id>）
        # 外部设备（Modbus/串口）映射到 custom:xxx 的读数一并合并，前端与联动拿到的都是有效值
        "custom_channels": services.custom.channels() if services.custom else [],
        "custom": _custom_values(),
        # 外部设备接入（Modbus/串口）：最近读数与设备清单
        "gateway": services.gateway.values() if services.gateway else {},
        "gateways": services.gateway.endpoints() if services.gateway else [],
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
    # limit 现在是"图上最大点数"：区间无论多长都会覆盖到最新数据，超长区间自动等距降采样
    return ok(database.query_water_history_chart(start, end, limit))


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
# 闭环回路（v2 卡片即来源）：一张「恒温闭环」卡 = 一路独立 PID，可多路并存（多水槽）。
@router.get("/water/pid/loops")
def pid_loops_get(_: dict = Depends(auth.require_perm("view_monitor"))):
    """各恒温闭环回路的运行状态（来源卡/执行器卡/目标/实测/占空比/错误原因）。"""
    loops = services.pid_loops.loops() if services.pid_loops else []
    # 回退说明：没有任何闭环卡时后端跑"默认回路"（加热槽温度 → 本机加热）
    return ok({"loops": loops, "using_default": not loops,
               "default_enabled": bool(store.pid_enabled()),
               "target_temp": store.target_temp()})


@router.post("/water/pid/loops")
def pid_loops_set(body: dict, user: dict = Depends(auth.require_perm("cfg_alarm"))):
    """更新某闭环回路的运行参数（目标温度 / Kp / Ki / Kd / 启用）——写回卡片配置。

    回路本身（用哪张温度卡、哪张执行器卡、可选的防干烧前置条件卡）在卡片配置里选。
    """
    if not services.pid_loops:
        return err(40003, "恒温闭环服务未启动")
    loop_id = str(body.get("id", "")).strip()
    if not loop_id:
        return err(40002, "必须指定回路 id（即闭环卡的卡片 id）")
    params: dict = {}
    try:
        if body.get("target") is not None:
            target = float(body["target"])
            if not (0 <= target <= 120):
                return err(40002, "目标温度须在 0~120 ℃")
            params["target"] = round(target, 2)
        for key in ("kp", "ki", "kd"):
            if body.get(key) is not None:
                value = float(body[key])
                if not (0 <= value <= 1000):
                    return err(40002, f"{key} 须在 0~1000")
                params[key] = round(value, 3)
    except (TypeError, ValueError):
        return err(40002, "目标温度与 Kp/Ki/Kd 必须为数值")
    if body.get("enabled") is not None:
        params["enabled"] = bool(body["enabled"])
    if not params:
        return err(40002, "没有需要更新的参数")
    try:
        loop = services.pid_loops.set_loop_params(loop_id, params)
    except KeyError:
        return err(40004, f"闭环回路不存在: {loop_id}（请确认该卡片仍在实时监控页）")
    except ValueError as exc:
        return err(40002, str(exc))
    database.insert_control_log("config.pid", "success",
                                f"更新恒温闭环[{loop.get('name') or loop_id}]参数: "
                                + ", ".join(f"{k}={v}" for k, v in params.items()),
                                operator=user.get("username") or None, source="manual")
    return ok({"loop": loop})


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


# ---------- 站点文案（浏览器标题/顶栏标题/副标题；登录页也需展示，故 GET 无需登录） ----------
SITE_DEFAULTS = {
    "title": "智能水循环监测与温控系统",
    "subtitle": "水循环监测 · 流量计量 · 定量浇水 · 报警 · 数据统计",
    "browser": "",   # 留空 = 同站点标题
}


@router.get("/water/site")
def site_get():
    cfg = database.get_all_config()
    out = dict(SITE_DEFAULTS)
    for k in SITE_DEFAULTS:
        v = cfg.get(f"site_{k}")
        if v:
            out[k] = v
    return ok(out)


@router.post("/water/site")
def site_set(body: dict, user: dict = Depends(auth.require_perm("cfg_system"))):
    for k in SITE_DEFAULTS:
        if k in body:
            database.set_config(f"site_{k}", str(body[k]).strip())
    database.insert_control_log("config.site", "success",
                                f"修改站点文案: {body.get('title', '')}"[:250],
                                operator=user.get("username") or None, source="manual")
    return site_get()


# ---------- 报警联动（v2 个体化：规则自带阈值，触发源/执行器均区分个体） ----------
def _validate_link_url(url: str, what: str):
    """联动相关 URL 的通用校验：http(s) + 代理白名单主机，非法抛 ValueError。"""
    u = urlparse(str(url or ""))
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"{what} 非法（仅支持 http/https）: {url}")
    if u.hostname not in config.DASHBOARD_PROXY_HOSTS:
        raise ValueError(f"{what} 目标主机不在白名单: {u.hostname}")


def _validate_link_actions(actions):
    """校验动作数组（触发与恢复共用）：动作必须指向实时监控页的卡片。"""
    if not isinstance(actions, list) or not actions:
        raise ValueError("动作列表不能为空")
    for a in actions:
        if not isinstance(a, dict):
            raise ValueError(f"非法联动动作: {a}")
        if not str(a.get("card", "")).strip():
            raise ValueError("每个动作必须指定实时监控页面上的卡片")
        kind = a.get("kind")
        if kind not in LINK_TARGET_KINDS:
            raise ValueError(f"非法联动动作: {a}")
        if kind == "quant":
            continue
        if a.get("state") not in ("on", "off"):
            raise ValueError(f"动作[{a.get('card')}] 必须指定开/关")
        if kind == "url":
            _validate_link_url(a.get("url"), "卡片指令 URL")


@router.get("/water/alarm/links")
def alarm_links_get(_: dict = Depends(auth.require_perm("cfg_alarm"))):
    if services.alarm:
        return ok({"links": services.alarm.links()})
    return ok({"links": []})


@router.post("/water/alarm/links")
def alarm_links_set(body: dict, user: dict = Depends(auth.require_perm("cfg_alarm"))):
    """联动规则 v3（卡片即来源）：触发源与动作都指向实时监控页的卡片。

    触发源 source_card + source{kind,url,path,period}：realtime 读本机快照、custom 由后端轮询；
    动作 {card,state,kind,url}：url 走白名单 GET，pump/heater 为本机执行器，quant 为取消定量。
    卡片定义以实时监控页为准（后端按 id 实时解析），这里同时保留一份快照用于兜底。
    """
    links = body.get("links")
    if not isinstance(links, list):
        return err(40002, "links 必须为数组")
    seen_ids: set[str] = set()
    try:
        for r in links:
            if not isinstance(r, dict):
                return err(40002, "联动规则格式错误")
            rid = str(r.get("id", "")).strip()
            if not rid or rid in seen_ids:
                return err(40002, "每条规则必须有唯一 id")
            seen_ids.add(rid)
            if not str(r.get("source_card", "")).strip():
                return err(40002, f"规则[{rid}] 必须选择实时监控页面上的触发源卡片")
            sensor = r.get("source")
            if not isinstance(sensor, dict) or sensor.get("kind") not in LINK_SOURCE_KINDS:
                return err(40002, f"触发源类型非法: {sensor}")
            if not str(sensor.get("path", "")).strip():
                return err(40002, f"规则[{rid}] 触发源必须填写取值路径")
            if sensor["kind"] == "custom":
                _validate_link_url(sensor.get("url"), "触发源接口 URL")
                try:
                    period = float(sensor.get("period") or 5)
                except (TypeError, ValueError):
                    return err(40002, f"规则[{rid}] 触发源轮询周期必须为数值")
                if not (2 <= period <= 300):
                    return err(40002, "触发源轮询周期须在 2~300 秒")
            try:
                if float(r.get("threshold")) != float(r.get("threshold")):
                    raise ValueError("阈值不能为空")
            except (TypeError, ValueError):
                return err(40002, f"规则[{rid}] 阈值必须为有效数值")
            if r.get("direction") not in LINK_DIRECTIONS:
                return err(40002, f"非法联动方向: {r.get('direction')}")
            try:
                hold = float(r.get("hold") or 0)
            except (TypeError, ValueError):
                return err(40002, f"规则[{rid}] 持续秒数必须为数值")
            if hold < 0 or hold > 3600:
                return err(40002, f"规则[{rid}] 持续秒数须在 0~3600")
            extras = r.get("extra") or []
            if not isinstance(extras, list):
                return err(40002, "附加条件必须为数组")
            if len(extras) > 5:
                return err(40002, "附加条件最多 5 条（同步评估，过多会拖慢采集循环）")
            for idx, ex in enumerate(extras):
                label = f"规则[{rid}] 附加条件{idx + 1}"
                if not isinstance(ex, dict) or not str(ex.get("card", "")).strip():
                    return err(40002, f"{label} 必须选择实时监控页面上的卡片")
                esrc = ex.get("source")
                if not isinstance(esrc, dict) or esrc.get("kind") not in LINK_SOURCE_KINDS:
                    return err(40002, f"{label} 触发源类型非法")
                if not str(esrc.get("path", "")).strip():
                    return err(40002, f"{label} 必须填写取值路径")
                if esrc["kind"] == "custom":
                    _validate_link_url(esrc.get("url"), f"{label} 接口 URL")
                if ex.get("direction") not in LINK_DIRECTIONS:
                    return err(40002, f"{label} 方向非法")
                try:
                    float(ex.get("threshold"))
                except (TypeError, ValueError):
                    return err(40002, f"{label} 阈值必须为数值")
            _validate_link_actions(r.get("actions"))
            recover = r.get("recover") or {}
            if recover.get("actions"):
                _validate_link_actions(recover["actions"])
    except ValueError as exc:
        return err(40002, str(exc))
    store.set_json("alarm_links", links)
    if services.alarm:
        services.alarm.reload_links()
    database.insert_control_log("config.link", "success",
                                f"保存报警联动规则 {len(links)} 条",
                                operator=user.get("username") or None, source="manual")
    return ok({"links": links})


# ---------- 自定义传感器通道历史/统计（通道由实时监控页的卡片声明派生） ----------
@router.get("/water/custom/history")
def custom_history(channel_id: str = Query(...),
                   start: str = Query(...), end: str = Query(...),
                   limit: int = Query(default=5000, ge=1, le=50000),
                   _: dict = Depends(auth.require_perm("view_history"))):
    try:
        _validate_range(start, end)
    except ValueError as exc:
        return err(40002, str(exc))
    return ok(database.query_custom_history_chart(channel_id, start, end, limit))


@router.get("/water/custom/stats")
def custom_stats(channel_id: str = Query(...),
                 start: str = Query(...), end: str = Query(...),
                 _: dict = Depends(auth.require_perm("view_history"))):
    try:
        _validate_range(start, end)
    except ValueError as exc:
        return err(40002, str(exc))
    return ok(database.custom_sensor_stats(channel_id, start, end))


# ---------- 历史回放（时间轴 + 按时间点取快照） ----------
@router.get("/water/replay")
def replay_at(ts: str = Query(..., description="目标时刻 yyyy-MM-dd HH:mm:ss"),
              _: dict = Depends(auth.require_perm("view_history"))):
    """回放某个时间点的系统快照：传感数据 + 各自定义通道该时刻最近值。"""
    try:
        datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return err(40002, f"时间格式错误: {ts}")
    row = database.query_sensor_at(ts)
    custom = {}
    for ch in (services.custom.channels() if services.custom else []):
        custom[ch["id"]] = database.query_custom_at(ch["id"], ts)
    gw = {}
    if services.gateway:
        for key in (services.gateway.values() or {}):
            if key.startswith("custom:"):
                cid = key.split(":", 1)[1]
                gw[cid] = database.query_custom_at(cid, ts)
    for key, value in gw.items():
        if custom.get(key) is None and value is not None:
            custom[key] = value
    return ok({"ts": ts, "sensor": row, "custom": custom})


@router.get("/water/replay/frames")
def replay_frames(start: str = Query(...), end: str = Query(...),
                  limit: int = Query(default=120, ge=2, le=600),
                  _: dict = Depends(auth.require_perm("view_history"))):
    """回放时间轴：区间内等距抽取若干帧（前端滑块/播放用）。"""
    try:
        _validate_range(start, end)
    except ValueError as exc:
        return err(40002, str(exc))
    # 帧序列覆盖整个区间（超长区间会等距降采样，保证含最新一帧）
    return ok(database.query_history_frames(start, end, limit))


# ---------- 通道标定（scale/offset 与两点标定） ----------
@router.get("/water/calibration")
def calibration_get(_: dict = Depends(auth.require_perm("view_device"))):
    """读取标定配置与本机可标定通道清单（现场换传感器后按需换算）。"""
    cal = calibration.load()
    custom = [{"key": c["id"], "name": c.get("name") or c["id"], "unit": c.get("unit") or ""}
              for c in (services.custom.channels() if services.custom else [])]
    return ok({"calibration": cal, "channels": list(calibration.CHANNELS), "custom": custom})


@router.post("/water/calibration")
def calibration_set(body: dict, user: dict = Depends(auth.require_perm("cfg_system"))):
    """保存标定：{calibration: {通道键: {scale, offset, unit, note}}}；未列出的通道恢复原值。"""
    cal = body.get("calibration")
    if not isinstance(cal, dict):
        return err(40002, "calibration 必须为对象")
    for key, item in cal.items():
        if not isinstance(item, dict):
            return err(40002, f"通道[{key}] 标定格式错误")
        try:
            scale = float(item.get("scale") if item.get("scale") is not None else 1)
            offset = float(item.get("offset") or 0)
        except (TypeError, ValueError):
            return err(40002, f"通道[{key}] 系数/偏移必须为数值")
        if not (-1_000_000 <= scale <= 1_000_000) or not (-1_000_000 <= offset <= 1_000_000):
            return err(40002, f"通道[{key}] 系数/偏移超出合理范围")
    store.set_json("channel_cal", cal)
    calibration.reload()
    database.insert_control_log("config.calib", "success", f"保存通道标定 {len(cal)} 项",
                                operator=user.get("username") or None, source="manual")
    return ok({"calibration": cal})


@router.post("/water/calibration/two_point")
def calibration_two_point(body: dict, _: dict = Depends(auth.require_perm("cfg_system"))):
    """两点标定：给两对「原始值 → 工程值」，返回 scale/offset（前端标定页调用）。"""
    try:
        result = calibration.two_point(body.get("raw1"), body.get("eng1"),
                                       body.get("raw2"), body.get("eng2"))
    except (TypeError, ValueError) as exc:
        return err(40002, f"两点标定失败: {exc}")
    return ok(result)


# ---------- 定时任务（简单 cron 式：每天某时刻 / 每 N 秒执行卡片动作） ----------
@router.get("/water/timers")
def timers_get(_: dict = Depends(auth.require_perm("cfg_alarm"))):
    if not services.timers:
        return ok({"timers": []})
    items = []
    for t in services.timers.timers():
        item = dict(t)
        item["next_run"] = services.timers.next_run(t)
        items.append(item)
    return ok({"timers": items})


@router.post("/water/timers")
def timers_set(body: dict, user: dict = Depends(auth.require_perm("cfg_alarm"))):
    """保存定时任务：interval（每 N 秒）或 daily（每天 HH:MM），动作与联动同构（卡片）。"""
    timers = body.get("timers")
    if not isinstance(timers, list):
        return err(40002, "timers 必须为数组")
    seen: set[str] = set()
    for t in timers:
        if not isinstance(t, dict):
            return err(40002, "定时任务格式错误")
        tid = str(t.get("id", "")).strip()
        if not tid or tid in seen:
            return err(40002, "每个定时任务必须有唯一 id")
        seen.add(tid)
        mode = t.get("mode")
        if mode not in ("interval", "daily"):
            return err(40002, f"任务[{tid}] 模式非法（interval / daily）")
        if mode == "interval":
            try:
                every = float(t.get("every_sec") or 600)
            except (TypeError, ValueError):
                return err(40002, f"任务[{tid}] 间隔必须为数值")
            if every < 5 or every > 86400:
                return err(40002, f"任务[{tid}] 间隔须在 5~86400 秒")
        else:
            at = str(t.get("at") or "")
            if len(at) != 5 or at[2] != ":" or not (at[:2].isdigit() and at[3:].isdigit()):
                return err(40002, f"任务[{tid}] 执行时刻须为 HH:MM")
            if not (0 <= int(at[:2]) <= 23 and 0 <= int(at[3:]) <= 59):
                return err(40002, f"任务[{tid}] 执行时刻超出范围")
        try:
            _validate_link_actions(t.get("actions"))
        except ValueError as exc:
            return err(40002, f"任务[{tid}] {exc}")
    store.set_json("timers", timers)
    if services.timers:
        services.timers.reload()
    database.insert_control_log("config.timers", "success", f"保存定时任务 {len(timers)} 条",
                                operator=user.get("username") or None, source="manual")
    return ok({"timers": timers})


# ---------- 外部设备接入（Modbus TCP / 串口服务器，现场只改这里） ----------
@router.get("/water/gateway")
def gateway_get(_: dict = Depends(auth.require_perm("view_device"))):
    return ok({"gateways": services.gateway.endpoints() if services.gateway else [],
               "values": services.gateway.values() if services.gateway else {},
               "errors": services.gateway.errors() if services.gateway else {}})


@router.post("/water/gateway")
def gateway_set(body: dict, user: dict = Depends(auth.require_perm("cfg_system"))):
    """保存外部设备接入配置：Modbus TCP（502）/ 串口服务器透明传输 / 本机串口。

    现场换传感器或执行器时，只需在这里填 IP、从站号、寄存器地址与换算系数。
    """
    gateways = body.get("gateways")
    if not isinstance(gateways, list):
        return err(40002, "gateways 必须为数组")
    seen: set[str] = set()
    for ep in gateways:
        if not isinstance(ep, dict):
            return err(40002, "外部设备配置格式错误")
        eid = str(ep.get("id", "")).strip()
        if not eid or eid in seen:
            return err(40002, "每台外部设备必须有唯一 id")
        seen.add(eid)
        mode = ep.get("mode")
        if mode not in MODES:
            return err(40002, f"设备[{eid}] 模式非法（tcp / rtu_tcp / rtu_serial）")
        if mode in (MODE_TCP, MODE_RTU_TCP):
            if not str(ep.get("host", "")).strip():
                return err(40002, f"设备[{eid}] 必须填写 IP/主机")
            try:
                port = int(ep.get("port") or 502)
            except (TypeError, ValueError):
                return err(40002, f"设备[{eid}] 端口必须为数值")
            if not (1 <= port <= 65535):
                return err(40002, f"设备[{eid}] 端口超出范围")
        elif not str(ep.get("device", "")).strip():
            return err(40002, f"设备[{eid}] 串口模式必须填写串口号（如 COM3）")
        try:
            unit = int(ep.get("unit") or 1)
        except (TypeError, ValueError):
            return err(40002, f"设备[{eid}] 从站号必须为数值")
        if not (1 <= unit <= 247):
            return err(40002, f"设备[{eid}] 从站号须在 1~247")
        for item in ep.get("polls") or []:
            if not isinstance(item, dict) or not str(item.get("target", "")).strip():
                return err(40002, f"设备[{eid}] 采集项必须指定目标通道")
            if int(item.get("func") or 3) not in FUNCS:
                return err(40002, f"设备[{eid}] 采集项功能码非法")
            if (item.get("type") or "u16") not in REG_TYPES:
                return err(40002, f"设备[{eid}] 采集项数据类型非法")
            try:
                int(item.get("addr") or 0)
                int(item.get("count") or 1)
                float(item.get("scale") if item.get("scale") is not None else 1)
                float(item.get("offset") or 0)
            except (TypeError, ValueError):
                return err(40002, f"设备[{eid}] 采集项地址/数量/系数必须为数值")
    store.set_json("gateways", gateways)
    if services.gateway:
        services.gateway.reload()
    database.insert_control_log("config.gateway", "success",
                                f"保存外部设备接入 {len(gateways)} 台",
                                operator=user.get("username") or None, source="manual")
    return ok({"gateways": gateways})


@router.get("/water/gateway/test")
def gateway_test(id: str = Query(..., description="外部设备 id"),
                 _: dict = Depends(auth.require_perm("cfg_system"))):
    """现场「试读一次」：立即按当前配置读一台设备，返回读数或失败原因。"""
    if not services.gateway:
        return err(40003, "适配器未启动")
    ep = next((e for e in services.gateway.endpoints() if e.get("id") == id), None)
    if not ep:
        return err(40004, f"外部设备不存在: {id}")
    try:
        return ok({"values": services.gateway.read_endpoint(ep)})
    except Exception as exc:  # noqa: BLE001
        return err(40003, f"读取失败: {exc}")


@router.get("/water/gateway/coil")
def gateway_coil(id: str = Query(...), addr: int = Query(..., ge=0),
                 state: str = Query(..., pattern="^(on|off)$"),
                 user: dict = Depends(auth.require_perm("ctrl_light"))):
    """写线圈（485 继电器板）：可直接作为控制卡片的开/关指令 URL 使用。"""
    if not services.gateway:
        return err(40003, "适配器未启动")
    try:
        result = services.gateway.write_coil(id, addr, state == "on")
    except Exception as exc:  # noqa: BLE001
        database.insert_control_log("device.gateway", "failed", f"外部设备线圈写入失败: {exc}",
                                    operator=user.get("username") or None, source="manual")
        return err(40003, f"写入失败: {exc}")
    database.insert_control_log("device.gateway", "success",
                                f"外部设备[{id}] 线圈 {addr} → {state.upper()}",
                                operator=user.get("username") or None, source="manual")
    return ok(result)


# ---------- 数据导出（CSV：赛题报表与 U 盘提交用） ----------
# 每类数据用各自的读取权限；文件带 UTF-8 BOM，Excel 双击不乱码。
_EXPORT_PERM = {"sensors": "view_history", "custom": "view_history",
                "alarms": "view_alarm", "logs": "view_log"}
_EXPORT_HEADERS = {
    "sensors": ["时间", "瞬时流量(L/min)", "累计水量(L)", "定量目标(L)", "水泵状态",
                "储水槽水温(℃)", "加热槽水温(℃)", "水压(kPa)", "加热状态", "光照(lx)"],
    "alarms": ["时间", "类型", "数值", "阈值", "方向", "消息", "状态"],
    "logs": ["时间", "动作", "结果", "详情", "操作人", "来源"],
    "custom": ["时间", "数值"],
}
_EXPORT_FIELDS = {
    "sensors": ["ts", "flow_rate", "total_flow", "pump_target", "pump_state",
                "storage_temp", "heater_temp", "pressure", "heater_state", "light"],
    "alarms": ["ts", "type", "value", "threshold", "direction", "message", "status"],
    "logs": ["ts", "action", "result", "detail", "operator", "source"],
    "custom": ["ts", "value"],
}


@router.get("/water/export.csv")
def export_csv(
    kind: str = Query(default="sensors", pattern="^(sensors|alarms|logs|custom)$"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    channel_id: str | None = Query(default=None),
    category: str | None = Query(default=None, pattern="^(device|config|account)$"),
    limit: int = Query(default=200000, ge=1, le=200000,
                       description="导出条数上限；超限时保留最新的数据（1Hz 下 20 万条约 55 小时）"),
    user: dict = Depends(auth.get_current_user),
):
    """导出 CSV。kind=sensors(传感数据)/alarms(告警)/logs(操作日志)/custom(自定义通道)。

    时间范围缺省为最近 24 小时；custom 必须给 channel_id。
    """
    need = _EXPORT_PERM[kind]
    if need not in (user.get("perms") or []):
        return err(40301, f"缺少权限: {need}")
    if kind == "custom" and not channel_id:
        return err(40002, "导出自定义通道数据必须指定 channel_id")
    if not end:
        end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not start:
        start = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        _validate_range(start, end)
    except ValueError as exc:
        return err(40002, str(exc))

    if kind == "sensors":
        rows = database.query_water_history(start, end, limit)
    elif kind == "custom":
        rows = database.query_custom_history(channel_id, start, end, limit)
    elif kind == "alarms":
        rows, _total = database.query_alarms(start, end, None, None, 1, limit)
    else:
        rows, _total = database.query_control_log(start, end, 1, limit, category)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_EXPORT_HEADERS[kind])
    for row in rows:
        writer.writerow(["" if row.get(f) is None else row.get(f) for f in _EXPORT_FIELDS[kind]])
    body = "\ufeff" + buf.getvalue()          # BOM：Excel 识别 UTF-8
    name = f"water_{kind}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    # 告知前端实际导出条数与区间总条数（超限时保留的是最新数据，前端据此提示）
    total = database.count_water_history(start, end) if kind == "sensors" else len(rows)
    return Response(content=body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "Cache-Control": "no-store",
                             "X-Exported-Rows": str(len(rows)),
                             "X-Total-Rows": str(total),
                             "Access-Control-Expose-Headers": "X-Exported-Rows, X-Total-Rows"})


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


@router.get("/water/judge/template")
def judge_template_get(_: dict = Depends(auth.require_perm("view_device"))):
    """读取判定服务地址与报文模板（现场按组委会规范填，免改代码）。"""
    return ok({"url": judge_service.judge_url(), "template": judge_service.load_template(),
               "default_url": config.JUDGE_URL, "device_id": config.JUDGE_DEVICE_ID})


@router.post("/water/judge/template")
def judge_template_set(body: dict, user: dict = Depends(auth.require_perm("cfg_system"))):
    """保存判定服务地址与报文模板：url / headers / report / poll / feedback。"""
    url = str(body.get("url", "") or "").strip()
    tpl = body.get("template") or {}
    if url and not url.startswith(("http://", "https://")):
        return err(40002, "判定服务地址需以 http:// 或 https:// 开头")
    if not isinstance(tpl, dict):
        return err(40002, "template 必须为对象")
    if tpl.get("headers") is not None and not isinstance(tpl["headers"], dict):
        return err(40002, "headers 必须为对象")
    for kind in ("report", "poll", "feedback"):
        section = tpl.get(kind)
        if section is None:
            continue
        if not isinstance(section, dict):
            return err(40002, f"{kind} 段必须为对象")
        path = str(section.get("path", "") or "")
        if path and not path.startswith("/"):
            return err(40002, f"{kind}.path 需以 / 开头（如 /api/report）")
        body_tpl = section.get("body")
        if body_tpl is not None and not isinstance(body_tpl, (dict, list)):
            return err(40002, f"{kind}.body 必须为对象或数组")
    store.set("judge_url", url)
    store.set_json("judge_template", tpl)
    database.insert_control_log("config.judge", "success",
                                f"保存判定服务模板（{'启用模板' if tpl else '清空模板'}）",
                                operator=user.get("username") or None, source="manual")
    return ok({"url": judge_service.judge_url(), "template": judge_service.load_template()})


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
    # 并发保护：前端载入这份布局时服务端返回的版本号（GET 的 rev）。
    # 网页端总是回传；测试脚本/接口直调可以不传，表示"不校验"（向后兼容）。
    base_rev: int | None = None


def _layout_rev() -> int:
    """当前布局版本号：每次保存/重置/恢复 +1，用于识别"拿着旧布局的页面"。"""
    try:
        return int(float(store.get("dashboard_layout_rev", "0") or 0))
    except (TypeError, ValueError):
        return 0


def _layout_backup_now() -> None:
    """把当前布局备份为「上一次布局」（保存/重置前调用），供一键恢复。

    这是防"误覆盖/被别的窗口覆盖"的安全网：布局是全局共享一份，
    任何一次保存都会让上一版留在 sys.dashboard_layout_prev。
    """
    current = store.get_json("dashboard_layout", None)
    if current is None:
        return
    store.set_json("dashboard_layout_prev", current)
    store.set("dashboard_layout_prev_ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def _layout_write(layout) -> int:
    """写入布局（先自动备份当前布局），返回新的版本号。layout=None 表示恢复默认（清空）。"""
    _layout_backup_now()
    if layout is None:
        store.set("dashboard_layout", "")
    else:
        store.set_json("dashboard_layout", layout)
    rev = _layout_rev() + 1
    store.set("dashboard_layout_rev", str(rev))
    return rev


def _reload_card_consumers() -> None:
    """仪表盘布局变化后，让依赖卡片的模块重新加载。

    自定义传感器通道（卡片 source.kind=custom 即声明）与报警联动规则
    （触发源/动作都指向卡片）都以下图为唯一来源，布局一变必须重载。
    """
    if services.custom:
        services.custom.reload()
    if services.alarm:
        services.alarm.reload_links()
    if services.pid_loops:
        services.pid_loops.reload()      # 闭环回路也由卡片派生（增删/改配置后重新加载）


@router.get("/water/dashboard/layout")
def dashboard_layout_get(_: dict = Depends(auth.require_perm("view_monitor"))):
    """读取全局仪表盘布局；从未保存过返回 layout=null（前端用内置默认布局）。

    rev 用于保存时回传（并发保护），has_prev/prev_ts 表示是否存在可恢复的上一次布局。
    """
    prev = store.get_json("dashboard_layout_prev", None)
    return ok({
        "layout": store.get_json("dashboard_layout", None),
        "rev": _layout_rev(),
        "has_prev": prev is not None,
        "prev_ts": store.get("dashboard_layout_prev_ts", None),
    })


@router.post("/water/dashboard/layout")
def dashboard_layout_set(body: DashboardLayoutRequest,
                         user: dict = Depends(auth.require_perm("cfg_system"))):
    """保存全局仪表盘布局（覆盖式，保存前自动备份上一版）。"""
    raw = json.dumps(body.layout, ensure_ascii=False)
    if len(raw) > 200_000:
        return err(40002, "布局配置过大（超过 200KB）")
    current_rev = _layout_rev()
    if body.base_rev is not None and body.base_rev != current_rev:
        # 页面载入的布局已被别人改过：拒绝保存，避免把别人的改动整片覆盖
        # （实测过：另一个停留在旧布局的页面点"完成"会把恢复回来的布局冲掉）
        return err(40009, f"仪表盘布局已被其他窗口修改（当前版本 {current_rev}，"
                          f"你载入的是 {body.base_rev}）：请刷新页面后重新编辑")
    rev = _layout_write(body.layout)
    database.insert_control_log(
        "config.dashboard", "success",
        f"更新仪表盘布局（{len(body.layout.get('widgets', []))} 张卡片）",
        operator=user.get("username") or None, source="manual")
    _reload_card_consumers()
    return ok({"layout": body.layout, "rev": rev})


@router.post("/water/dashboard/layout/reset")
def dashboard_layout_reset(user: dict = Depends(auth.require_perm("cfg_system"))):
    """清除已保存布局，前端回退为内置默认布局（清除前同样备份当前布局）。"""
    rev = _layout_write(None)
    database.insert_control_log("config.dashboard", "success", "恢复默认仪表盘布局",
                                operator=user.get("username") or None, source="manual")
    _reload_card_consumers()
    return ok({"layout": None, "rev": rev})


@router.post("/water/dashboard/layout/restore_prev")
def dashboard_layout_restore_prev(user: dict = Depends(auth.require_perm("cfg_system"))):
    """一键恢复「上一次布局」（保存/重置前自动备份的那一份）。

    恢复本身也会备份当前布局，所以可以反复点击在最近两版之间来回切换——
    用于误删卡片、被别的窗口覆盖等突发情况的一键回退。
    """
    prev = store.get_json("dashboard_layout_prev", None)
    if prev is None:
        return err(40004, "没有可恢复的上一次布局（本机尚未保存过布局）")
    rev = _layout_write(prev)
    count = len(prev.get("widgets", []) or [])
    database.insert_control_log("config.dashboard", "success",
                                f"恢复上一次仪表盘布局（{count} 张卡片）",
                                operator=user.get("username") or None, source="manual")
    _reload_card_consumers()
    return ok({"layout": prev, "rev": rev})


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
