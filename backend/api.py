"""HTTP 业务接口（FastAPI 路由）：灯杆列表/详情/历史/统计/控制/视频流/人员监测/告警/日志。

统一返回 {"code":0,"msg":"ok","data":...}；前端页面通过 /api/* 调用，
接口文档在启动后访问 /docs 自动生成。
"""
import asyncio
import time
import urllib.error
from datetime import datetime

import cv2
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import auth
import config
import database
import infer
import store
from state import services

router = APIRouter(prefix="/api")


def ok(data=None) -> dict:
    return {"code": 0, "msg": "ok", "data": data}


def err(code: int, msg: str, data=None) -> dict:
    return {"code": code, "msg": msg, "data": data}


class ControlRequest(BaseModel):
    action: str = Field(pattern="^(on|off)$")


class ValveRequest(BaseModel):
    action: str = Field(pattern="^(on|off)$")


class AlarmConfigRequest(BaseModel):
    temp_max: float | None = Field(default=None, ge=-50, le=100)
    temp_min: float | None = Field(default=None, ge=-50, le=100)
    humidity_max: float | None = Field(default=None, ge=0, le=100)
    humidity_min: float | None = Field(default=None, ge=0, le=100)
    luminance_max: float | None = Field(default=None, ge=0, le=200000)
    luminance_min: float | None = Field(default=None, ge=0, le=200000)
    # 烟雾浓度（MQ-2 AO 原始值）上下限
    smoke_max: float | None = Field(default=None, ge=0, le=4095)
    smoke_min: float | None = Field(default=None, ge=0, le=4095)
    # 人员数量告警规则
    person_alert_enabled: int | None = Field(default=None, ge=0, le=1)
    person_alert_min: float | None = Field(default=None, ge=1, le=100)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    role: str = Field(default="viewer", max_length=32)


class UserRoleRequest(BaseModel):
    role: str = Field(min_length=1, max_length=32)


class UserPasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class UserStatusRequest(BaseModel):
    status: str = Field(pattern="^(active|disabled)$")


class SysConfigRequest(BaseModel):
    """系统配置页提交：可带任意一节，未带的节保持不变。"""
    lamp_posts: list | None = None
    service: dict | None = None
    sensor_fields: dict | None = None
    lamp_ctrl_default: dict | None = None
    valve: dict | None = None
    soil: dict | None = None


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


def _get_lamp(lamp_id: str):
    lamp = services.lamps.get(lamp_id) if services.lamps else None
    if lamp is None:
        raise LookupError(f"灯杆不存在: {lamp_id}")
    return lamp


def _lamp_summary(lamp) -> dict:
    snap = lamp.snapshot()
    alarms = services.get_lamp_alarms(lamp.id)
    return {**snap, "alarm_count": len(alarms), "active_alarms": alarms}


# ---------- 灯杆列表与详情 ----------
@router.get("/lampposts")
def lampposts(_: dict = Depends(auth.require_perm("view_monitor"))):
    lamps = services.lamps.all() if services.lamps else []
    return ok({"lampposts": [_lamp_summary(l) for l in lamps]})


@router.get("/lampposts/{lamp_id}")
def lamppost_detail(lamp_id: str, _: dict = Depends(auth.require_perm("view_monitor"))):
    try:
        lamp = _get_lamp(lamp_id)
    except LookupError as exc:
        return err(40004, str(exc))
    return ok(_lamp_summary(lamp))


# ---------- 历史数据与统计 ----------
@router.get("/lampposts/{lamp_id}/history")
def history(
    lamp_id: str,
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    limit: int = Query(default=5000, ge=1, le=50000),
    _: dict = Depends(auth.require_perm("view_history")),
):
    try:
        _validate_range(start, end)
    except ValueError as exc:
        return err(40002, str(exc))
    points = database.query_history(lamp_id, start, end, limit)
    return ok({"points": points})


@router.get("/lampposts/{lamp_id}/stats")
def stats(
    lamp_id: str,
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    _: dict = Depends(auth.require_perm("view_history")),
):
    try:
        _validate_range(start, end)
    except ValueError as exc:
        return err(40002, str(exc))
    return ok(database.query_stats(lamp_id, start, end))


# ---------- 设备控制（灯光） ----------
@router.post("/lampposts/{lamp_id}/control")
def control(lamp_id: str, body: ControlRequest, _: dict = Depends(auth.require_perm("ctrl_light"))):
    try:
        lamp = _get_lamp(lamp_id)
    except LookupError as exc:
        return err(40004, str(exc))
    t0 = time.perf_counter()
    try:
        lamp.set_light(body.action)
    except Exception as exc:  # noqa: BLE001
        return err(40003, f"控制指令执行失败: {exc}")
    exec_ms = round((time.perf_counter() - t0) * 1000, 1)
    database.insert_control_log(lamp_id, body.action, "success", f"响应 {exec_ms}ms")
    return ok(_lamp_summary(lamp))


@router.post("/lampposts/{lamp_id}/valve")
def valve_control(lamp_id: str, body: ValveRequest, _: dict = Depends(auth.require_perm("ctrl_light"))):
    """阀门（舵机）开关：开阀转开启角度，关阀转关闭角度（角度在配置页设置）。"""
    try:
        lamp = _get_lamp(lamp_id)
    except LookupError as exc:
        return err(40004, str(exc))
    t0 = time.perf_counter()
    try:
        lamp.set_valve(body.action)
    except Exception as exc:  # noqa: BLE001
        return err(40003, f"阀门控制指令执行失败: {exc}")
    exec_ms = round((time.perf_counter() - t0) * 1000, 1)
    database.insert_control_log(lamp_id, f"valve_{body.action}", "success", f"响应 {exec_ms}ms")
    return ok(_lamp_summary(lamp))


# ---------- 视频流 ----------
@router.get("/lampposts/{lamp_id}/video")
def video(lamp_id: str, _: dict = Depends(auth.require_perm("view_monitor"))):
    try:
        lamp = _get_lamp(lamp_id)
    except LookupError as exc:
        return err(40004, str(exc))

    async def gen():
        # 异步生成器：不占线程池线程，客户端断开时 Starlette 会立即取消并回收连接
        try:
            while True:
                frame = lamp.video.get_frame()
                if frame is None:
                    await asyncio.sleep(0.08)
                    continue
                ok_flag, buf = await asyncio.to_thread(cv2.imencode, ".jpg", frame)
                if not ok_flag:
                    continue
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                       + buf.tobytes() + b"\r\n")
                await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            raise

    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# ---------- 截图与人员智能监测 ----------
@router.get("/lampposts/{lamp_id}/snapshot")
def snapshot(lamp_id: str, _: dict = Depends(auth.require_perm("view_monitor"))):
    """截图并返回 base64 编码（不触发识别）。"""
    try:
        lamp = _get_lamp(lamp_id)
    except LookupError as exc:
        return err(40004, str(exc))
    frame = lamp.video.get_frame()
    if frame is None:
        return err(40005, "视频流暂无画面")
    try:
        dataurl = infer.frame_to_dataurl(frame)
    except ValueError as exc:
        return err(40005, str(exc))
    return ok({"image": dataurl, "lamp_id": lamp_id, "ts": database.now_str()})


@router.post("/lampposts/{lamp_id}/detect")
def detect(lamp_id: str, _: dict = Depends(auth.require_perm("view_detect"))):
    """手动截图并调用 AI 识别接口进行人员监测，记录并返回结果。"""
    try:
        lamp = _get_lamp(lamp_id)
    except LookupError as exc:
        return err(40004, str(exc))
    frame = lamp.video.get_frame()
    if frame is None:
        return err(40005, "视频流暂无画面，无法截图")
    try:
        original = infer.frame_to_dataurl(frame)
        result = infer.call_infer(original)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return err(50000, f"智能识别服务不可达或调用失败: {exc}")

    inference_results = result.get("inference_results", []) or []
    processed = result.get("processed_image")
    person_count = len(inference_results)
    max_conf = 0.0
    for item in inference_results:
        max_conf = max(max_conf, float(item.get("confidence", 0.0)))

    ts = datetime.now()
    detection_id = database.insert_detection(
        lamp_id, ts, person_count, round(max_conf, 4), original, processed,
    )
    return ok({
        "id": detection_id,
        "lamp_id": lamp_id,
        "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "person_count": person_count,
        "max_confidence": round(max_conf, 4),
        "inference_results": inference_results,
        "original_image": original,
        "processed_image": processed,
    })


@router.get("/lampposts/{lamp_id}/detect/current")
def detect_current(lamp_id: str, _: dict = Depends(auth.require_perm("view_detect"))):
    """返回持续自动识别的最新结果摘要（供前端实时展示人数）。"""
    try:
        lamp = _get_lamp(lamp_id)
    except LookupError as exc:
        return err(40004, str(exc))
    detector = services.lamps.detector(lamp_id) if services.lamps else None
    result = detector.last_result() if detector else None
    resp = {
        "lamp_id": lamp_id,
        "ts": result["ts"] if result else None,
        "person_count": result["person_count"] if result else None,
        "max_confidence": result["max_confidence"] if result else None,
        "alarm_active": bool(result["alarm_active"]) if result else False,
        "enabled": bool(services.alarm.thresholds.get("person_alert_enabled")) if services.alarm else False,
    }
    return ok(resp)


@router.get("/lampposts/{lamp_id}/detect_video")
def detect_video(lamp_id: str, _: dict = Depends(auth.require_perm("view_detect"))):
    """持续自动识别的标注视频流（MJPEG）。"""
    try:
        lamp = _get_lamp(lamp_id)
    except LookupError as exc:
        return err(40004, str(exc))

    async def gen():
        # 异步生成器：无标注帧时睡眠等待；客户端断开立即取消，避免线程/连接泄漏
        try:
            while True:
                detector = services.lamps.detector(lamp_id) if services.lamps else None
                # 取实时原始帧并在本地绘制最新识别框：全帧率实时标注，
                # 不再依赖每 5 秒一次的整张标注图往返传输
                frame = lamp.video.get_frame()
                if frame is None:
                    await asyncio.sleep(0.5)
                    continue
                if detector is not None:
                    frame = detector.annotate(frame)
                ok_flag, buf = await asyncio.to_thread(cv2.imencode, ".jpg", frame)
                if not ok_flag:
                    await asyncio.sleep(0.5)
                    continue
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                       + buf.tobytes() + b"\r\n")
                await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            raise

    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# ---------- 人员监测记录 ----------
@router.get("/detections")
def detections(
    lamp_id: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=200),
    _: dict = Depends(auth.require_perm("view_detect")),
):
    items, total = database.query_detections(lamp_id, start, end, keyword, page, page_size)
    return ok({"items": items, "total": total, "page": page, "page_size": page_size})


@router.get("/detections/{detection_id}")
def detection_detail(detection_id: int, _: dict = Depends(auth.require_perm("view_detect"))):
    row = database.get_detection(detection_id)
    if row is None:
        return err(40004, "监测记录不存在")
    return ok(row)


# ---------- 告警 ----------
@router.get("/alarm/config")
def alarm_config_get(_: dict = Depends(auth.require_perm("view_alarm"))):
    thresholds = services.alarm.thresholds if services.alarm else {}
    return ok({"thresholds": thresholds, "active_count": services.active_count()})


@router.post("/alarm/config")
def alarm_config_set(body: AlarmConfigRequest, _: dict = Depends(auth.require_perm("cfg_alarm"))):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return err(40002, "未提供任何阈值")
    for key, value in updates.items():
        database.set_config(key, str(value))
    if services.alarm:
        services.alarm.reload()
    return ok({"thresholds": services.alarm.thresholds})


@router.get("/alarms")
def alarms(
    lamp_id: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=200),
    _: dict = Depends(auth.require_perm("view_alarm")),
):
    items, total = database.query_alarms(lamp_id, start, end, type, status, keyword, page, page_size)
    return ok({"items": items, "total": total, "page": page, "page_size": page_size})


@router.get("/alarm/stats")
def alarm_stats(lamp_id: str | None = Query(default=None), _: dict = Depends(auth.require_perm("view_alarm"))):
    """按类型统计告警数量（全库或指定灯杆），供告警分布图使用。"""
    rows = database.query_alarm_stats(lamp_id)
    return ok({"stats": rows})


@router.get("/alarms/{alarm_id}")
def alarm_detail(alarm_id: int, _: dict = Depends(auth.require_perm("view_alarm"))):
    """单条告警详情（含异常情况截图快照图）。"""
    row = database.get_alarm(alarm_id)
    if row is None:
        return err(40004, "告警记录不存在")
    return ok(row)


# ---------- 操作日志 ----------
@router.get("/logs")
def logs(
    lamp_id: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=200),
    _: dict = Depends(auth.require_perm("view_log")),
):
    items, total = database.query_control_log(lamp_id, start, end, keyword, page, page_size)
    return ok({"items": items, "total": total, "page": page, "page_size": page_size})


# ---------- 系统配置（配置中心页面） ----------
@router.get("/sysconfig")
def sysconfig_get(_: dict = Depends(auth.require_perm("cfg_system"))):
    """返回全部可在前端编辑的配置（含当前生效值）。"""
    lamps = services.lamps.all() if services.lamps else []
    return ok({
        "lamp_posts": store.lamp_posts(),
        "lamp_ctrl_default": store.lamp_ctrl_default(),
        "infer_url": store.infer_url(),
        "infer_timeout": store.infer_timeout(),
        "person_detect_interval": store.person_detect_interval(),
        "sample_interval": store.sample_interval(),
        "monitor_interval": store.monitor_interval(),
        "monitor_timeout": store.monitor_timeout(),
        "sensor_timeout": store.sensor_timeout(),
        "sensor_ttl": store.sensor_ttl(),
        "sensor_trip": store.sensor_trip(),
        "sensor_cooldown": store.sensor_cooldown(),
        "sensor_fields": store.sensor_fields(),
        "valve": {
            "open_angle": store.valve_open_angle(),
            "close_angle": store.valve_close_angle(),
            "valve_ctrl_default": store.valve_ctrl_default(),
        },
        "soil": {
            "auto_close_enabled": int(bool(services.alarm.thresholds.get("soil_auto_close_enabled"))) if services.alarm else 0,
            "close_threshold": float(services.alarm.thresholds.get("soil_close_threshold", 60.0)) if services.alarm else 60.0,
        },
        "running_lamps": [l.id for l in lamps],
    })


@router.post("/sysconfig")
def sysconfig_set(body: SysConfigRequest, _: dict = Depends(auth.require_perm("cfg_system"))):
    """分节保存配置：灯杆增量重建 / 服务参数即时生效 / 传感器格式重建灯杆生效。"""
    msgs: list[str] = []
    changed: list[str] = []

    if body.lamp_posts is not None:
        for p in body.lamp_posts:
            if not str(p.get("id", "")).strip():
                return err(40002, "灯杆 ID 不能为空")
            p.setdefault("name", "")
            p.setdefault("location", "")
            p.setdefault("rtsp_url", "")
            p.setdefault("sensor_url", "")
            p.setdefault("esp32_base", "")
        store.set_json("lamp_posts", body.lamp_posts)
        if services.lamps:
            changed = services.lamps.reload(body.lamp_posts)
        msgs.append(f"灯杆配置已保存，重建/删除: {', '.join(changed) if changed else '无'}")

    if body.service is not None:
        svc = body.service
        try:
            if "infer_url" in svc:
                store.set("infer_url", str(svc["infer_url"]))
            if "infer_timeout" in svc:
                store.set("infer_timeout", float(svc["infer_timeout"]))
            if "person_detect_interval" in svc:
                store.set("person_detect_interval", max(1.0, float(svc["person_detect_interval"])))
            if "sample_interval" in svc:
                store.set("sample_interval", max(0.5, float(svc["sample_interval"])))
            if "monitor_interval" in svc:
                store.set("monitor_interval", max(5.0, float(svc["monitor_interval"])))
            if "monitor_timeout" in svc:
                store.set("monitor_timeout", max(0.5, float(svc["monitor_timeout"])))
            if "sensor_timeout" in svc:
                store.set("sensor_timeout", max(0.5, min(60.0, float(svc["sensor_timeout"]))))
            if "sensor_ttl" in svc:
                store.set("sensor_ttl", max(0.5, min(30.0, float(svc["sensor_ttl"]))))
            if "sensor_trip" in svc:
                store.set("sensor_trip", max(1.0, min(20.0, float(svc["sensor_trip"]))))
            if "sensor_cooldown" in svc:
                store.set("sensor_cooldown", max(1.0, min(300.0, float(svc["sensor_cooldown"]))))
        except (TypeError, ValueError):
            return err(40002, "服务参数中存在非法数值")
        msgs.append("服务参数已保存，即时生效")

    if body.sensor_fields is not None:
        sf = dict(body.sensor_fields)
        for key in ("status", "temperature", "humidity", "light"):
            if not str(sf.get(key, "")).strip():
                return err(40002, f"传感器格式缺少字段: {key}")
        # 烟雾字段可选：未填写则回退默认（适配无 MQ-2 的设备）
        sf.setdefault("smoke", "smokeRaw")
        sf.setdefault("smoke_alarm", "smokeAlarm")
        # 地面湿度字段可选：留空则删除键，读取时回退默认 soilRaw/soilMoisture
        for key in ("soil_raw", "soil_moisture"):
            if key in sf:
                v = str(sf.get(key) or "").strip()
                if v:
                    sf[key] = v
                else:
                    sf.pop(key, None)
        store.set_json("sensor_fields", sf)
        if services.lamps:
            # 字段映射变化 → 所有灯杆指纹变化 → 重建（生效）
            changed = services.lamps.reload()
        msgs.append("传感器格式已保存并生效")

    if body.lamp_ctrl_default is not None:
        store.set_json("lamp_ctrl_default", body.lamp_ctrl_default)
        if services.lamps:
            # 全局灯控格式变化 → 未单独配置灯控接口的灯杆指纹变化 → 重建
            changed = services.lamps.reload()
        msgs.append("灯控全局默认格式已保存并生效")

    if body.valve is not None:
        v = body.valve
        try:
            open_a = float(v.get("open_angle", store.valve_open_angle()))
            close_a = float(v.get("close_angle", store.valve_close_angle()))
            if not (0 <= open_a <= 180 and 0 <= close_a <= 180):
                raise ValueError
            store.set("valve_open_angle", str(open_a))
            store.set("valve_close_angle", str(close_a))
        except (TypeError, ValueError):
            return err(40002, "阀门角度必须是 0~180 的数字")
        if "valve_ctrl_default" in v:
            vc = dict(v["valve_ctrl_default"])
            vc.setdefault("path", "/api/servo/set")
            vc.setdefault("angle_param", "angle")
            vc.setdefault("status", "status")
            vc.setdefault("field", "angle")
            vc.setdefault("method", "GET")
            store.set_json("valve_ctrl_default", vc)
            if services.lamps:
                # 阀门接口格式变化 → 灯杆指纹变化 → 重建生效
                changed = services.lamps.reload()
        msgs.append("阀门配置已保存并生效")

    if body.soil is not None:
        s = body.soil
        try:
            enabled = 1.0 if s.get("auto_close_enabled") else 0.0
            threshold = float(s.get("close_threshold", 60.0))
            if not (0 <= threshold <= 100):
                raise ValueError
            database.set_config("soil_auto_close_enabled", str(enabled))
            database.set_config("soil_close_threshold", str(threshold))
            if services.alarm:
                services.alarm.reload()
        except (TypeError, ValueError):
            return err(40002, "地面湿度阈值必须是 0~100 的数字")
        msgs.append("地面湿度配置已保存并生效")

    return ok({"msg": "；".join(msgs), "changed": changed})


# ---------- 系统状态 ----------
@router.get("/system")
def system_status(_: dict = Depends(auth.require_perm("view_device"))):
    lamps = services.lamps.all() if services.lamps else []
    return ok({
        "lamp_count": len(lamps),
        "lamps": [_lamp_summary(l) for l in lamps],
        "uptime_s": round(services.uptime, 1),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "infer_url": store.infer_url(),
        "active_alarm_count": services.active_count(),
        "devices": services.device_status(),
    })


# ---------- 账号与权限 ----------
def _user_public(row: dict) -> dict:
    """用户数据对外脱敏：不返回密码哈希。"""
    return {k: v for k, v in row.items() if k != "password_hash"}


@router.post("/auth/login")
def login(body: LoginRequest):
    """登录：校验用户名密码，签发 JWT。"""
    row = database.get_user(body.username)
    if row is None or not auth.verify_password(body.password, row["password_hash"]):
        return err(40006, "用户名或密码错误")
    if row["status"] != "active":
        return err(40006, "账号已被禁用")
    token = auth.make_token(row["username"], row["role"])
    perms = auth.perms_of(row["role"])
    return ok({
        "token": token,
        "user": {"username": row["username"], "role": row["role"], "perms": perms},
    })


@router.get("/auth/me")
def auth_me(user: dict = Depends(auth.get_current_user)):
    """返回当前登录用户信息（前端启动时校验登录态）。"""
    return ok({"username": user["username"], "role": user["role"],
               "perms": user["perms"], "role_label": auth.role_label(user["role"])})


@router.post("/auth/change_password")
def change_password(body: UserPasswordRequest, user: dict = Depends(auth.get_current_user)):
    """当前登录用户修改自己的密码。"""
    row = database.get_user(user["username"])
    if row is None:
        return err(40004, "用户不存在")
    database.update_user_password(row["id"], auth.hash_password(body.password))
    return ok({"msg": "密码已修改"})


@router.get("/auth/users")
def auth_users(_: dict = Depends(auth.require_perm("account_manage"))):
    rows = database.query_users()
    return ok({"users": [_user_public(r) for r in rows]})


@router.post("/auth/users")
def auth_create_user(body: UserCreateRequest, _: dict = Depends(auth.require_perm("account_manage"))):
    username = body.username.strip()
    if not username:
        return err(40002, "用户名不能为空")
    if database.get_user(username) is not None:
        return err(40002, "用户名已存在")
    if body.role not in auth.role_matrix():
        return err(40002, f"角色不存在: {body.role}")
    user_id = database.insert_user(username, auth.hash_password(body.password), body.role)
    return ok({"id": user_id, "msg": f"已创建用户 {username}"})


@router.post("/auth/users/{user_id}/password")
def auth_reset_password(user_id: int, body: UserPasswordRequest,
                        _: dict = Depends(auth.require_perm("account_manage"))):
    """管理员重置指定用户密码。"""
    database.update_user_password(user_id, auth.hash_password(body.password))
    return ok({"msg": "密码已重置"})


@router.post("/auth/users/{user_id}/role")
def auth_set_role(user_id: int, body: UserRoleRequest,
                  _: dict = Depends(auth.require_perm("account_manage"))):
    if body.role not in auth.role_matrix():
        return err(40002, f"角色不存在: {body.role}")
    database.update_user_role(user_id, body.role)
    return ok({"msg": "角色已更新"})


@router.post("/auth/users/{user_id}/status")
def auth_set_status(user_id: int, body: UserStatusRequest,
                    user: dict = Depends(auth.require_perm("account_manage"))):
    rows = database.query_users()
    target = next((r for r in rows if r["id"] == user_id), None)
    if target is None:
        return err(40004, "用户不存在")
    if target["username"] == user["username"]:
        return err(40002, "不能禁用自己")
    if target["username"] == config.ADMIN_USERNAME:
        return err(40002, "不能禁用管理员账号")
    database.update_user_status(user_id, body["status"])
    return ok({"msg": "状态已更新"})


@router.delete("/auth/users/{user_id}")
def auth_delete_user(user_id: int, user: dict = Depends(auth.require_perm("account_manage"))):
    rows = database.query_users()
    target = next((r for r in rows if r["id"] == user_id), None)
    if target is None:
        return err(40004, "用户不存在")
    if target["username"] == user["username"]:
        return err(40002, "不能删除自己")
    if target["username"] == config.ADMIN_USERNAME:
        return err(40002, "不能删除管理员账号")
    database.delete_user(user_id)
    return ok({"msg": "用户已删除"})


@router.get("/auth/roles")
def auth_roles(_: dict = Depends(auth.require_perm("account_manage"))):
    """返回权限点定义与角色矩阵（供前端矩阵勾选）。"""
    return ok({
        "permissions": auth.PERMISSIONS,
        "roles": auth.role_matrix(),
    })


@router.post("/auth/roles")
def auth_save_roles(body: dict, user: dict = Depends(auth.require_perm("account_manage"))):
    """保存角色矩阵：{角色: [权限key, ...]} 或 {角色: (显示名, 权限列表)}。"""
    matrix = {}
    for key, val in body.get("roles", {}).items():
        if not str(key).strip():
            continue
        if isinstance(val, (list, tuple)):
            matrix[str(key)] = [str(v) for v in val]
        elif isinstance(val, dict):
            label = val.get("label", key)
            plist = val.get("perms")
            matrix[str(key)] = [label, "*" if plist == "*" else [str(p) for p in (plist or [])]]
        else:
            return err(40002, f"角色 {key} 格式错误")
    # 安全兜底：admin 角色必须存在且拥有全部权限
    matrix.setdefault("admin", auth.DEFAULT_ROLES["admin"])
    if matrix.get("admin") != auth.DEFAULT_ROLES["admin"]:
        matrix["admin"] = auth.DEFAULT_ROLES["admin"]
    auth.save_role_matrix(matrix)
    return ok({"msg": "角色矩阵已保存", "roles": auth.role_matrix()})
