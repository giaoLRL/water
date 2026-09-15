"""FastAPI 应用入口：初始化数据库/设备/告警/PID/判定，启动1Hz采集循环。

主流程(每周期)：
    采集 → 恒温控制(PID) → 入库 → 告警 → 判定上报 → 判定指令执行
用后台线程循环，避免阻塞 uvicorn 事件循环。
启动: python main.py（默认 http://0.0.0.0:8000）
"""
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
from alarm import AlarmEngine
from api import router
from auth import ensure_admin
from judge_service import JudgeService
from pid_control import PID, heater_action
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
        for k in ("flow_rate", "total_liters"):
            if ing.get(k) is not None:
                data[k] = float(ing[k])
        data["total_flow"] = data.get("total_liters")

    # 恒温闭环控制(PID)：需设备同时支持温度与加热通道，当前固件不支持故跳过（任务六）
    if config.pid_supported() and store.pid_enabled():
        target = store.target_temp()
        duty = services.pid.update(target, data["heater_temp"], store.period())
        act = heater_action(duty)
        if act != plant.heater_state:
            plant.heater_control(act)
            name = "开启加热" if act == "on" else "关闭加热"
            database.insert_control_log(f"heater_{act}", "success",
                                        f"恒温闭环自动{name}(目标{target}℃,占空比{duty:.0f}%)",
                                        operator=None, source="auto")
            if DEBUG_ENABLE:
                _log.info("[CONTROL] 恒温闭环 → %s (heater_temp=%.2f target=%.1f duty=%.0f%%)",
                          name, data["heater_temp"], target, duty)

    # 数据入库(任务三：持久化)；设备不支持的通道写 NULL
    database.insert_water_sensor(
        datetime.now(),
        data.get("storage_temp"), data.get("heater_temp"),
        data.get("flow_rate"), data.get("pressure"),
        data.get("pump_state") or "unknown", data.get("heater_state"),
        data.get("total_flow"), data.get("pump_target"),
    )

    # 告警检查(任务六异常告警)：仅检查设备实际支持的通道
    services.alarm.check(data)

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
        _log.info("[SENSOR] flow=%s L/min total=%s L pump=%s target=%.2f online=%s%s",
                  _fmt(data.get("flow_rate")), _fmt(data.get("total_liters"), 3),
                  data.get("pump_state"), float(data.get("pump_target") or 0.0),
                  data.get("sensor_online"),
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
    database.init_database()
    ensure_admin()
    services.plant = WaterPlant()
    services.alarm = AlarmEngine()
    services.pid = PID(store.pid_kp(), store.pid_ki(), store.pid_kd())
    services.judge = JudgeService()
    enabled = [k for k, v in config.DEVICE_FEATURES.items() if v]
    _log.info("采集设备: %s (超时%.1fs) 支持通道: %s", config.DEVICE_URL, config.DEVICE_TIMEOUT_S, ",".join(enabled))
    if not config.pid_supported():
        _log.info("恒温闭环(PID)已禁用：当前固件未提供温度/加热通道")
    threading.Thread(target=collect_loop, daemon=True).start()
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

    uvicorn.run(app, host="0.0.0.0", port=8000)