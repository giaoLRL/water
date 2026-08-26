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
from alarm import AlarmEngine
from api import router
from device import LampManager
from monitor import DeviceMonitor
from state import services


async def collect_loop() -> None:
    """周期采样所有灯杆：写历史库 + 异常告警检查。"""
    while True:
        try:
            now = datetime.now()
            for lamp in services.lamps.all():
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
                active = services.alarm.check(lamp.id, sensors)
                services.set_lamp_alarms(lamp.id, active)
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