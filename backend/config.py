"""全局配置：数据库、灯杆列表、识别接口、告警阈值等关键参数，支持环境变量覆盖。

各模块统一通过 import config 读取配置；默认值可直接修改，也可在启动前设置
IOT_DB_* / LAMP_* / INFER_* 等环境变量覆盖，无需改动代码。
"""
import os
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return _env(key, str(default)).lower() in ("1", "true", "yes", "on")


# ---------- 数据库 ----------
DB_HOST = _env("IOT_DB_HOST", "127.0.0.1")
DB_PORT = _env_int("IOT_DB_PORT", 3306)
DB_USER = _env("IOT_DB_USER", "iot_user")
DB_PASSWORD = _env("IOT_DB_PASSWORD", "iot_pass_2026")
DB_NAME = _env("IOT_DB_NAME", "iot_system")
DB_POOL_SIZE = _env_int("IOT_DB_POOL_SIZE", 12)

# ---------- 采集 ----------
SAMPLE_INTERVAL = _env_float("LAMP_SAMPLE_INTERVAL", 2.0)

# ---------- 机房列表 ----------
# 每个机房作为独立监控单元；rtsp_url 为空时使用模拟视频画面。
LAMP_POSTS = [
    {
        "id": "01",
        "name": "机房01",
        "location": "机房A区",
        "rtsp_url": "rtsp://admin:123456@192.168.31.201/stream0",
        "sensor_url": "http://192.168.31.100/api/data",  # ESP32 + DHT11 真实温湿度
        "esp32_base": "http://192.168.31.100",           # ESP32 灯控服务（MOS 继电器）
    },
    {
        "id": "02",
        "name": "机房02",
        "location": "机房B区",
        "rtsp_url": "",
    },
    {
        "id": "03",
        "name": "机房03",
        "location": "机房C区",
        "rtsp_url": "",
    },
]

# ---------- 人员智能识别接口（YOLO） ----------
INFER_URL = _env("LAMP_INFER_URL", "http://127.0.0.1:5000/infer")
INFER_TIMEOUT = _env_float("LAMP_INFER_TIMEOUT", 60.0)
PERSON_DETECT_INTERVAL = _env_float("LAMP_PERSON_DETECT_INTERVAL", 1.0)

# ---------- 账号与令牌 ----------
JWT_SECRET = _env("IOT_JWT_SECRET", "iot-smart-lamp-secret")
TOKEN_TTL = _env_int("IOT_TOKEN_TTL", 12 * 3600)          # 令牌有效期（秒），默认 12 小时
ADMIN_USERNAME = _env("IOT_ADMIN_USER", "admin")
ADMIN_DEFAULT_PASSWORD = _env("IOT_ADMIN_PASSWORD", "admin123")   # 首次登录后请尽快修改

# ---------- 告警阈值默认值 ----------
DEFAULT_THRESHOLDS = {
    "temp_max": 38.0,
    "temp_min": -5.0,
    "humidity_max": 85.0,
    "humidity_min": 20.0,
    "luminance_max": 80000.0,
    "luminance_min": 0.0,
    # 烟雾浓度（MQ-2 AO 原始值 0~4095）上下限
    "smoke_max": 3000.0,
    "smoke_min": 0.0,
    # 人员数量告警规则：识别到人数 >= person_alert_min 时告警
    "person_alert_min": 3.0,
    "person_alert_enabled": 1.0,
}

# ---------- 路径 ----------
BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = Path(_env("IOT_FRONTEND_DIR", str(BASE_DIR.parent / "frontend")))


def db_dsn() -> dict:
    """返回 PyMySQL 连接参数字典。"""
    return {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "database": DB_NAME,
        "charset": "utf8mb4",
        "autocommit": True,
    }
