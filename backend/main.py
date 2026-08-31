"""FastAPI 应用入口：初始化数据库 / 灯杆 / 告警引擎 / 设备监控，并启动采样循环。

- lifespan 启动阶段完成资源初始化，创建采集任务；
- 采集循环把各灯杆的同步工作（真实传感器 HTTP、数据库写入）放进线程池并行执行，
  避免阻塞 uvicorn 异步事件循环，保证视频流与接口不因传感器慢而周期卡顿。
启动: python main.py（默认 http://0.0.0.0:8000）
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_log = logging.getLogger("main")
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles as StarletteStaticFiles

import config
import database
import infer
import store
from alarm import AlarmEngine
from api import router
from auth import ensure_admin
from device import LampManager
from monitor import DeviceMonitor
from state import services


def _collect_lamp(lamp) -> None:
    """单灯杆一次同步采集（在线程池执行）：写历史库 + 异常告警检查。

    涉及网络请求（真实传感器 HTTP）与数据库写入，均为同步阻塞操作，
    必须在线程池中执行，避免阻塞 uvicorn 异步事件循环（否则所有视频流/接口会周期性卡顿）。
    """
    now = datetime.now()
    sensors = lamp.read_sensors()
    snap = lamp.snapshot()
    database.insert_sensor_data(
        lamp.id,
        now,
        snap["temperature"],
        snap["humidity"],
        snap["luminance"],
        snap["light_state"],
        smoke=snap["smoke"],
        smoke_alarm=snap["smoke_alarm"],
    )
    # 异常快照：取当前视频帧（仅在触发新告警时写库）
    frame = lamp.video.get_frame()
    image = infer.frame_to_dataurl(frame) if frame is not None else None
    # 真实传感器（ESP32）各指标在线状态：离线时告警引擎跳过数值阈值检查，避免 0 值假告警
    online = None
    if getattr(lamp, "sensor_source", "") == "esp32":
        online = {
            "temperature": lamp.sensor_online,
            "humidity": lamp.sensor_online,
            "luminance": lamp.light_online,
            "smoke": lamp.smoke_online,
        }
    active = services.alarm.check(lamp.id, sensors, image=image, online=online)
    # 设备不在线告警：指标/传感器掉线本身即告警，全部在线自动恢复
    active.extend(services.alarm.check_offline(lamp.id, snap, image=image))
    services.set_lamp_alarms(lamp.id, active)
    # 周期性读回真实灯状态（ESP32），保持页面显示与物理一致
    lamp.sync_light()


async def collect_loop() -> None:
    """周期采样所有灯杆：各灯杆并行投入线程池，事件循环不被同步阻塞。"""
    while True:
        try:
            lamps = list(services.lamps.all())
            loop = asyncio.get_running_loop()
            await asyncio.gather(
                *(loop.run_in_executor(None, _collect_lamp, lamp) for lamp in lamps)
            )
        except Exception:  # noqa: BLE001
            _log.exception("采集循环异常，跳过本轮")
            await asyncio.sleep(1.0)  # 异常后短暂冷却，避免高频重试
        # 采样间隔可在系统配置页调整（每轮读取，即时生效）
        await asyncio.sleep(store.sample_interval())


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_database()
    # 首次启动创建默认管理员账号（admin/admin123），已存在则跳过
    ensure_admin()
    services.lamps = LampManager()
    services.alarm = AlarmEngine()
    services.monitor = DeviceMonitor()
    services.monitor.start()

    task = asyncio.create_task(collect_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="基于物联网的分布式机房监控系统", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=200,
        content={"code": 40002, "msg": "参数错误", "data": str(exc.errors())},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api"):
        # 认证/权限错误用专属错误码区分（未登录 40101，无权限 40301）
        if exc.status_code == 401:
            return JSONResponse(
                status_code=200,
                content={"code": 40101, "msg": f"未登录或登录已过期: {exc.detail}", "data": None},
            )
        if exc.status_code == 403:
            return JSONResponse(
                status_code=200,
                content={"code": 40301, "msg": f"无权限执行此操作: {exc.detail}", "data": None},
            )
        return JSONResponse(
            status_code=200,
            content={"code": 40002, "msg": f"接口不存在或请求错误: {exc.detail}", "data": None},
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


class NoCacheStaticFiles(StarletteStaticFiles):
    """静态资源彻底禁用缓存：no-store 强制不缓存，避免浏览器拿到旧版前端文件。"""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp


app.mount("/", NoCacheStaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
