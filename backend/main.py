"""FastAPI 应用入口：初始化数据库/设备/告警/PID/判定，启动1Hz采集循环。

主流程(每周期)：
    采集 → 恒温控制(PID) → 入库 → 告警 → 判定上报 → 判定指令执行
用后台线程循环，避免阻塞 uvicorn 事件循环。
启动: python main.py（默认 http://0.0.0.0:8000）
"""
import os
import sys

# =============================================================
# 0) 启动自检：必须在导入业务依赖之前跑（只依赖标准库）
#    目的：环境没配好时也要**留下日志**并给出可执行的修复建议，
#    而不是只抛一个看不懂的 traceback（依赖缺失/数据库连不上/端口被占用…）。
#    日志文件：仓库根目录 .runtime/startup.log（控制台同时打印）
# =============================================================
import preflight

_PORT = 8000
if len(sys.argv) > 1 and sys.argv[1].isdigit():
    _PORT = int(sys.argv[1])
_PORT = int(os.environ.get("WATER_PORT", _PORT))

_PREFLIGHT_ITEMS = preflight.run_checks(port=_PORT if __name__ == "__main__" else None)
if not preflight.report(_PREFLIGHT_ITEMS):
    sys.exit(2)     # 详细原因与修复建议已打印并写入 .runtime/startup.log

# -------------------------------------------------------------
# 1) 业务依赖导入（自检已确认它们都在；真出错也有日志兜底）
# -------------------------------------------------------------
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles as StarletteStaticFiles

import config
import database
import store
import water_device
from alarm import AlarmEngine
from custom_channels import CustomChannelManager
from api import router
from auth import ensure_admin
from judge_service import JudgeService
from gateway import ModbusGateway
from scheduler import TimerScheduler
from pid_control import PID, heater_action
from pid_loops import PidLoopManager
from state import services
from water_device import WaterPlant

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_log = logging.getLogger("water")

# 调试开关：1=打印每次采集与控制动作，0=仅打印错误，比赛现场可关
DEBUG_ENABLE = 1


def run_cycle() -> None:
    """执行一个采集周期(采集设备→可选PID→入库→告警→判定)。"""
    plant = services.plant
    data = plant.read()

    # 采集端主动上报数据优先(可选链路，任务二)；无则用后端轮询到的值
    ing = services.ingest
    if ing and isinstance(ing, dict) and (time.time() - ing.get("ts", 0)) < 2.0:
        for k in ("flow_rate", "total_liters", "storage_temp", "heater_temp", "pressure", "light"):
            if ing.get(k) is not None:
                data[k] = float(ing[k])
        data["total_flow"] = data.get("total_liters")

    # 自定义传感器通道：按各自周期轮询 → 入库 → 值并入 data["custom"]（全链路）
    # 传入本轮快照：卡片声明的本机路径（如 device.rssi / tank.tanks.*）也在这里取样入库
    data["tank"] = water_device.tank_snapshot(data)   # 与 API 快照同一份口径，供卡片路径引用
    services.custom.poll(data)
    data["custom"] = services.custom.values()

    # 外部设备适配器（Modbus TCP / 串口服务器）：读数覆盖同名通道，custom:xxx 写入自定义通道
    if services.gateway:
        services.gateway.poll()
        gvals = services.gateway.values()
        if gvals:
            custom = dict(data.get("custom") or {})
            for key, value in gvals.items():
                if key.startswith("custom:"):
                    custom[key.split(":", 1)[1]] = value
                else:
                    data[key] = value
            data["custom"] = custom
            data["gateway"] = gvals

    # 恒温闭环控制(PID)：一张「恒温闭环」卡 = 一路独立闭环（多水槽可并存）；
    # 未配置闭环卡时走默认回路（加热槽温度 → 本机加热），与旧行为一致。
    if services.pid_loops:
        services.pid_loops.tick(data)

    # 数据入库(任务三：持久化)；设备不支持的通道写 NULL
    database.insert_water_sensor(
        datetime.now(),
        data.get("storage_temp"), data.get("heater_temp"),
        data.get("flow_rate"), data.get("pressure"),
        data.get("pump_state") or "unknown", data.get("heater_state"),
        data.get("total_flow"), data.get("pump_target"),
        data.get("light"),
    )

    # 告警检查(任务六异常告警)：仅检查设备实际支持的通道
    services.alarm.check(data)
    # 定时任务：到点执行卡片动作（与联动同一套执行器与审计）
    if services.timers:
        services.timers.tick()
    # 报警联动：本机通道规则随 check 评估；自定义传感器源按各自周期在此轮询
    services.alarm.check_custom()

    # 判定服务：上报 + 轮询指令 + 执行反馈(任务五)
    judge = services.judge
    judge.report(data)
    judge.poll()
    cmd = judge.take_command()
    if cmd:
        device = str(cmd.get("device") or cmd.get("type") or "")
        action = str(cmd.get("action") or cmd.get("command") or "")
        if action in ("on", "off"):
            try:
                if device == "pump":
                    plant.pump_control(action)
                elif device == "heater":
                    plant.heater_control(action)
                database.insert_control_log(f"{device}_{action}", "success", "判定服务指令执行",
                                            operator=None, source="judge")
            except Exception as exc:  # noqa: BLE001
                database.insert_control_log(f"{device}_{action}", "failed",
                                            f"判定服务指令执行失败: {exc}",
                                            operator=None, source="judge")
        judge.feedback("ok", {"pump_state": plant.pump_state, "heater_state": plant.heater_state})

    if DEBUG_ENABLE:
        _log.info("[SENSOR] flow=%s L/min total=%s L pump=%s target=%.2f online=%s "
                  "t1=%s℃ t2=%s℃ p=%s kPa heater=%s lv=%s%% lx=%s%s",
                  _fmt(data.get("flow_rate")), _fmt(data.get("total_liters"), 3),
                  data.get("pump_state"), float(data.get("pump_target") or 0.0),
                  data.get("sensor_online"),
                  _fmt(data.get("storage_temp")), _fmt(data.get("heater_temp")),
                  _fmt(data.get("pressure"), 1), data.get("heater_state"),
                  _fmt(data.get("level_heater"), 1), _fmt(data.get("light"), 1),
                  f" err={data.get('last_error')}" if data.get("last_error") else "")


def _fmt(value, digits: int = 2) -> str:
    """把可能为 None 的读数格式化为字符串，None 显示 '--'。"""
    return "--" if value is None else f"{float(value):.{digits}f}"


def collect_loop() -> None:
    """采集线程：按配置周期循环执行采集周期。"""
    while True:
        try:
            run_cycle()
        except Exception:  # noqa: BLE001
            _log.exception("[ERROR] 采集循环异常，跳过本轮")
            time.sleep(1.0)
        time.sleep(store.period())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化失败（数据库掉线、建表失败、账号初始化失败…）也要留日志，而不是只抛 traceback
    try:
        database.init_database()
        ensure_admin()
        services.plant = WaterPlant()
        services.alarm = AlarmEngine()
        services.alarm.plant = services.plant   # 报警联动动作的执行对象
        services.timers = TimerScheduler(services.alarm)   # 定时任务（复用联动动作执行器）
        services.custom = CustomChannelManager()  # 自定义传感器通道（全链路）
        services.pid = PID(store.pid_kp(), store.pid_ki(), store.pid_kd())
        services.pid_loops = PidLoopManager(services.plant)   # 多路恒温闭环（卡片即来源）
        services.judge = JudgeService()
        services.gateway = ModbusGateway()   # 外部设备（Modbus/串口）接入适配器
        enabled = [k for k, v in config.DEVICE_FEATURES.items() if v]
        _log.info("采集设备: %s (超时%.1fs) 支持通道: %s", config.DEVICE_URL, config.DEVICE_TIMEOUT_S, ",".join(enabled))
        if not config.pid_supported():
            _log.info("恒温闭环(PID)已禁用：当前固件未提供温度/加热通道")
        preflight.log("启动初始化完成：数据库/建表/账号/采集线程就绪")
        threading.Thread(target=collect_loop, daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        preflight.log_exception("启动初始化失败（数据库/建表/账号等），服务未启动", exc)
        raise
    try:
        yield
    finally:
        pass


app = FastAPI(title="智能水循环监测与温控物联网系统", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=200, content={"code": 40002, "msg": "参数错误", "data": str(exc.errors())})


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api"):
        if exc.status_code == 401:
            return JSONResponse(status_code=200, content={"code": 40101, "msg": f"未登录或登录已过期: {exc.detail}", "data": None})
        if exc.status_code == 403:
            return JSONResponse(status_code=200, content={"code": 40301, "msg": f"无权限执行此操作: {exc.detail}", "data": None})
        return JSONResponse(status_code=200, content={"code": 40002, "msg": f"接口不存在或请求错误: {exc.detail}", "data": None})
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


class NoCacheStaticFiles(StarletteStaticFiles):
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp


app.mount("/", NoCacheStaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    # 端口：python main.py [端口] > 环境变量 WATER_PORT > 默认 8000
    uvicorn.run(app, host="0.0.0.0", port=_PORT)
