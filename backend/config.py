"""全局配置：数据库、采集周期、告警阈值、恒温PID、判定服务、模拟参数。

所有比赛现场可能需要修改的参数都集中在本文件，用【现场修改】标注。
各业务模块统一 import config 读取；可被 IOT_* / WATER_* 环境变量覆盖。
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


# ---------- 数据库 ----------
DB_HOST = _env("IOT_DB_HOST", "127.0.0.1")
DB_PORT = _env_int("IOT_DB_PORT", 3306)
DB_USER = _env("IOT_DB_USER", "iot_user")
DB_PASSWORD = _env("IOT_DB_PASSWORD", "iot_pass_2026")
DB_NAME = _env("IOT_DB_NAME", "iot_system")
DB_POOL_SIZE = _env_int("IOT_DB_POOL_SIZE", 8)

# =========================================================
# 比赛现场需要修改的参数（集中管理，勿散落到各处）
# =========================================================
SAMPLE_PERIOD_S = _env_float("WATER_SAMPLE_PERIOD", 1.0)      # 【现场修改】采集周期(秒)，须≥1Hz

# 采集端来源：留空=本机模拟数据；填真实接口地址则读取真实传感器(温/流/压)
SENSOR_URL = _env("WATER_SENSOR_URL", "")                     # 【现场修改】真实采集端HTTP，如 http://192.168.x.x/api/data
CTRL_URL = _env("WATER_CTRL_URL", "")                         # 【现场修改】真实继电器控制端HTTP，如 http://192.168.x.x/api

# 告警阈值(可在运行期"系统配置"页修改，数据库 config 表覆盖此处默认值)
DEFAULT_THRESHOLDS = {
    "storage_temp_max": 45.0,   # 【现场修改】储水槽温度上限℃
    "storage_temp_min": 0.0,    # 【现场修改】储水槽温度下限℃
    "heater_temp_max": 60.0,    # 【现场修改】加热槽温度上限℃
    "heater_temp_min": 0.0,     # 【现场修改】加热槽温度下限℃
    "flow_max": 14.0,           # 【现场修改】流量上限 L/min
    "flow_min": 0.0,            # 【现场修改】流量下限 L/min
    "pressure_max": 120.0,      # 【现场修改】压力上限 kPa
    "pressure_min": 10.0,       # 【现场修改】压力下限 kPa
}

# 恒温闭环 PID（任务六：本地恒温控制，稳态±1℃内）
TARGET_TEMP_DEFAULT = _env_float("WATER_TARGET_TEMP", 42.0)   # 【现场修改】目标温度℃
PID_KP = _env_float("WATER_PID_KP", 16.0)                     # 【现场修改】PID 比例系数
PID_KI = _env_float("WATER_PID_KI", 0.3)                      # 【现场修改】PID 积分系数
PID_KD = _env_float("WATER_PID_KD", 25.0)                     # 【现场修改】PID 微分系数
PID_ENABLED_DEFAULT = _env_int("WATER_PID_ENABLED", 0)        # 【现场修改】恒温闭环是否默认开启(0关1开)

# 组委会智能判定服务（任务五：数据上报/指令轮询/结果反馈，现场按裁判公告填写）
JUDGE_ENABLED_DEFAULT = _env_int("WATER_JUDGE_ENABLED", 0)    # 【现场修改】是否启用上报(0关1开)
JUDGE_URL = _env("WATER_JUDGE_URL", "")                       # 【现场修改】判定服务地址，如 http://127.0.0.1:9000
JUDGE_DEVICE_ID = _env("WATER_JUDGE_DEVICE_ID", "WATER_01")   # 【现场修改】本设备上报ID
JUDGE_REPORT_PATH = _env("WATER_JUDGE_REPORT_PATH", "/api/report")   # 【现场修改】上报接口路径
JUDGE_POLL_PATH = _env("WATER_JUDGE_POLL_PATH", "/api/poll")         # 【现场修改】指令轮询路径
JUDGE_FEEDBACK_PATH = _env("WATER_JUDGE_FEEDBACK_PATH", "/api/feedback")  # 【现场修改】结果反馈路径
JUDGE_REPORT_PERIOD_S = _env_float("WATER_JUDGE_REPORT_PERIOD", 1.0)   # 【现场修改】上报周期(s)=1
JUDGE_POLL_PERIOD_S = _env_float("WATER_JUDGE_POLL_PERIOD", 1.0)       # 【现场修改】轮询周期(s)

# 模拟水路参数（没有真实采集端时用于演示）
SIM_AMBIENT_TEMP = _env_float("WATER_SIM_AMBIENT", 22.0)      # 【现场修改】室温(自然散热目标)
SIM_FLOW_PUMP = _env_float("WATER_SIM_FLOW_PUMP", 6.5)        # 【现场修改】水泵开启流量 L/min
SIM_HEATER_RATE = _env_float("WATER_SIM_HEATER_RATE", 0.35)   # 【现场修改】加热槽升温速率 ℃/s
SIM_COOL_RATE = _env_float("WATER_SIM_COOL_RATE", 0.04)       # 【现场修改】自然降温速率
SIM_NOISE = _env_float("WATER_SIM_NOISE", 0.15)               # 【现场修改】传感器噪声幅度
SIM_PRESSURE_BASE = _env_float("WATER_SIM_PRESSURE_BASE", 35.0)  # 【现场修改】压力基准 kPa

# 账号与令牌
JWT_SECRET = _env("IOT_JWT_SECRET", "iot-water-secret")
TOKEN_TTL = _env_int("IOT_TOKEN_TTL", 12 * 3600)
ADMIN_USERNAME = _env("IOT_ADMIN_USER", "admin")
ADMIN_DEFAULT_PASSWORD = _env("IOT_ADMIN_PASSWORD", "admin123")

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