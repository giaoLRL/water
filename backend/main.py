"""FastAPI 入口：初始化数据库/灯杆/告警，启动后台采集循环。"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

import config
import database
import infer
from alarm import AlarmEngine
from api import router
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
    )
    # 异常快照：取当前视频帧（仅在触发新告警时写库）
    frame = lamp.video.get_frame()
    image = infer.frame_to_dataurl(frame) if frame is not None else None
    active = services.alarm.check(lamp.id, sensors, image=image)
    services.set_lamp_alarms(lamp.id, active)


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
            pass
        await asyncio.sleep(config.SAMPLE_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_database()
    services.lamps = LampManager()
    services.alarm = AlarmEngine()
    services.monitor = DeviceMonitor()
    services.monitor.start()

    task = asyncio.create_task(collect_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="基于物联网的分布式智慧灯杆监控系统", lifespan=lifespan)
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
        return JSONResponse(
            status_code=200,
            content={"code": 40002, "msg": f"接口不存在或请求错误: {exc.detail}", "data": None},
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)