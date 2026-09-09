"""HTTP 业务接口（FastAPI 路由）：水循环实时/历史/控制/统计/告警/PID/判定 + 账号鉴权。

统一返回 {"code":0,"msg":"ok","data":...}；前端通过 /api/* 调用，
接口文档启动后访问 /docs 自动生成。
"""
from datetime import datetime

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
    action: str = Field(pattern="^(on|off)$")


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
    storage_temp_max: float | None = Field(default=None, ge=-50, le=110)
    storage_temp_min: float | None = Field(default=None, ge=-50, le=110)
    heater_temp_max: float | None = Field(default=None, ge=-50, le=110)
    heater_temp_min: float | None = Field(default=None, ge=-50, le=110)
    flow_max: float | None = Field(default=None, ge=0, le=100)
    flow_min: float | None = Field(default=None, ge=0, le=100)
    pressure_max: float | None = Field(default=None, ge=0, le=500)
    pressure_min: float | None = Field(default=None, ge=0, le=500)


class IngestRequest(BaseModel):
    storage_temp: float | None = None
    heater_temp: float | None = None
    flow_rate: float | None = None
    pressure: float | None = None


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


def _snapshot() -> dict:
    """实时快照：最新传感数据+设备状态+活跃告警。"""
    plant = services.plant
    data = plant.read() if plant else {}
    alarms = services.alarm.active_alarms() if services.alarm else []
    return {
        "storage_temp": data.get("storage_temp"),
        "heater_temp": data.get("heater_temp"),
        "flow_rate": data.get("flow_rate"),
        "pressure": data.get("pressure"),
        "total_flow": data.get("total_flow"),
        "pump_state": data.get("pump_state"),
        "heater_state": data.get("heater_state"),
        "sensor_online": data.get("sensor_online"),
        "target_temp": store.target_temp(),
        "pid_enabled": bool(store.pid_enabled()),
        "pid_duty": round(services.pid.last_duty, 1) if services.pid else 0.0,
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
def _control(device: str, body: ActionRequest, perm: str):
    if services.plant is None:
        return err(40004, "设备未初始化")
    t0 = datetime.now()
    try:
        if device == "pump":
            services.plant.pump_control(body.action)
        else:
            services.plant.heater_control(body.action)
    except Exception as exc:  # noqa: BLE001
        return err(40003, f"控制指令执行失败: {exc}")
    database.insert_control_log(
        f"{device}_{body.action}", "success",
        f"手动控制 { '水泵' if device == 'pump' else '加热模块' } {body.action}",
    )
    return ok(_snapshot())


@router.post("/water/pump")
def pump(body: ActionRequest, _: dict = Depends(auth.require_perm("ctrl_light"))):
    return _control("pump", body, "ctrl_light")


@router.post("/water/heater")
def heater(body: ActionRequest, _: dict = Depends(auth.require_perm("ctrl_light"))):
    return _control("heater", body, "ctrl_light")


# ---------- 采集端上报(任务二：底层→后端 HTTP 传输) ----------
@router.post("/water/ingest")
def ingest(body: IngestRequest, _: dict = Depends(auth.require_perm("view_monitor"))):
    """采集端(ESP32/上位机)每周期把真实传感数据 POST 到此接口。"""
    services.ingest = {**body.model_dump(exclude_none=True), "ts": datetime.now().timestamp()}
    return ok({"msg": "已接收采集端数据"})


# ---------- 恒温PID(任务六) ----------
@router.post("/water/target")
def set_target(body: TargetRequest, _: dict = Depends(auth.require_perm("cfg_alarm"))):
    store.set("target_temp", body.temp)
    if services.pid:
        services.pid.reset()
    return ok({"target_temp": store.target_temp()})


@router.post("/water/pid/mode")
def pid_mode(body: PidModeRequest, _: dict = Depends(auth.require_perm("cfg_alarm"))):
    store.set("pid_enabled", body.enabled)
    if services.pid:
        services.pid.reset()
    return ok({"pid_enabled": bool(store.pid_enabled())})


@router.get("/water/pid")
def pid_get(_: dict = Depends(auth.require_perm("view_history"))):
    return ok({
        "target_temp": store.target_temp(),
        "pid_enabled": bool(store.pid_enabled()),
        "kp": store.pid_kp(), "ki": store.pid_ki(), "kd": store.pid_kd(),
    })


@router.post("/water/pid")
def pid_set(body: PidParamsRequest, _: dict = Depends(auth.require_perm("cfg_alarm"))):
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
def period_set(body: PeriodRequest, _: dict = Depends(auth.require_perm("cfg_system"))):
    store.set("period", body.period)
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
def alarm_config_set(body: AlarmConfigRequest, _: dict = Depends(auth.require_perm("cfg_alarm"))):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return err(40002, "未提供任何阈值")
    for key, value in updates.items():
        database.set_config(key, str(value))
    if services.alarm:
        services.alarm.reload()
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
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=200),
    _: dict = Depends(auth.require_perm("view_log")),
):
    items, total = database.query_control_log(start, end, page, page_size)
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
    return ok({
        "uptime_s": round(services.uptime, 1),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "period": store.period(),
        "database_ok": database.ping(),
        "sensor_url": config.SENSOR_URL or "模拟",
        "active_alarm_count": len(services.alarm.active_alarms()) if services.alarm else 0,
    })


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


@router.post("/auth/users")
def auth_create_user(body: dict, _: dict = Depends(auth.require_perm("account_manage"))):
    username = str(body.get("username", "")).strip()
    if not username:
        return err(40002, "用户名不能为空")
    if database.get_user(username) is not None:
        return err(40002, "用户名已存在")
    role = str(body.get("role", "viewer"))
    if role not in auth.role_matrix():
        return err(40002, f"角色不存在: {role}")
    user_id = database.insert_user(username, auth.hash_password(str(body.get("password", ""))), role)
    return ok({"id": user_id, "msg": f"已创建用户 {username}"})


@router.post("/auth/users/{user_id}/password")
def auth_reset_password(user_id: int, body: dict, _: dict = Depends(auth.require_perm("account_manage"))):
    database.update_user_password(user_id, auth.hash_password(str(body.get("password", ""))))
    return ok({"msg": "密码已重置"})


@router.post("/auth/users/{user_id}/role")
def auth_set_role(user_id: int, body: dict, _: dict = Depends(auth.require_perm("account_manage"))):
    if body.get("role") not in auth.role_matrix():
        return err(40002, f"角色不存在: {body.get('role')}")
    database.update_user_role(user_id, body["role"])
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
    return ok({"msg": "用户已删除"})


@router.get("/auth/roles")
def auth_roles(_: dict = Depends(auth.require_perm("account_manage"))):
    return ok({"permissions": auth.PERMISSIONS, "roles": auth.role_matrix()})


@router.post("/auth/roles")
def auth_save_roles(body: dict, _: dict = Depends(auth.require_perm("account_manage"))):
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
    return ok({"msg": "角色矩阵已保存", "roles": auth.role_matrix()})