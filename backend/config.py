"""全局配置：现场采集设备(ESP32)、数据库、采集周期、告警阈值、判定服务、账号。

所有比赛现场可能需要修改的参数都集中在本文件，用【现场修改】标注。
各业务模块统一 import config 读取；可被 IOT_* / WATER_* 环境变量覆盖。

数据来源（纯真实模式）：
    全部传感/执行数据均来自现场 ESP32 采集端（默认 http://192.168.31.100），
    后端不再生成模拟数据；设备离线时相关字段返回 None，前端显示“设备离线”。
    设备能力由 DEVICE_FEATURES 声明，未支持的通道一律不采集、不告警、不展示。
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
SAMPLE_PERIOD_S = _env_float("WATER_SAMPLE_PERIOD", 1.0)   # 【现场修改】采集周期(秒)，须≥1Hz

# ---------- 现场采集/控制设备(ESP32) ----------
# 【现场修改】采集控制端根地址，不带尾部斜杠；接口路径由 esp32_client 拼接
DEVICE_URL = _env("WATER_DEVICE_URL", "http://192.168.31.100").rstrip("/")
DEVICE_TIMEOUT_S = _env_float("WATER_DEVICE_TIMEOUT", 1.5)        # 单次HTTP请求超时(秒)
DEVICE_HEALTH_PERIOD_S = _env_float("WATER_DEVICE_HEALTH_PERIOD", 10.0)  # 读取设备健康信息周期(秒)
DEVICE_OFFLINE_AFTER_S = _env_float("WATER_DEVICE_OFFLINE_AFTER", 5.0)   # 连续无响应多久判定离线

# 设备能力表（对应现场固件实际提供的通道）。
# 现场固件为《YF-S401 水流量检测》：只提供 流量 / 累计水量 / 水泵 / 定量目标 / 清零，
# 不含温度、压力、加热继电器，故这三项为 False。
# 【现场修改】若后续烧录了带 2 路温度/压力/加热 的固件，把对应项改成 True 即可：
# 采集、告警、接口与前端会自动启用该通道，无需改动其他代码。
#
# level_* = 液位通道：双水槽循环加热系统，储水槽/加热槽各一路液位，
#   传感器即将加装、目前**尚未接入**。
#   False → 界面水位使用模拟值（见 SIM_LEVEL_*，明确标注"模拟"）；
#   True  → 从对应 LEVEL_PATH_* 读取真实液位，界面自动标注"实测"。
DEVICE_FEATURES = {
    "flow": True,
    "volume": True,
    "pump": True,
    "pump_target": True,
    "volume_reset": True,
    "level_storage": False,
    "level_heater": False,
    "temperature": False,
    "pressure": False,
    "heater": False,
}

# ---------- 双水槽液位通道（传感器尚未接入，接口路径待固件确定后填写） ----------
TANKS = ("storage", "heater")
TANK_LABELS = {"storage": "储水槽", "heater": "加热槽"}

LEVEL_PATH_STORAGE = _env("WATER_LEVEL_PATH_STORAGE", "/api/level/storage")   # 【现场修改】
LEVEL_PATH_HEATER = _env("WATER_LEVEL_PATH_HEATER", "/api/level/heater")      # 【现场修改】
TANK_HEIGHT_CM_STORAGE = _env_float("WATER_TANK_HEIGHT_CM_STORAGE", 100.0)    # 【现场修改】水槽高度(cm)
TANK_HEIGHT_CM_HEATER = _env_float("WATER_TANK_HEIGHT_CM_HEATER", 100.0)      # 【现场修改】水槽高度(cm)

# 水槽容积(L)：把液位百分比换算成"估算水量"显示用；按现场水槽实际容积填写。
TANK_CAPACITY_STORAGE_DEFAULT = _env_float("WATER_TANK_CAPACITY_STORAGE", 1000.0)   # 【现场修改】
TANK_CAPACITY_HEATER_DEFAULT = _env_float("WATER_TANK_CAPACITY_HEATER", 1000.0)     # 【现场修改】

# 模拟液位（对应通道为 False 时使用，仅用于演示，非真实液位）
# 双水槽按「水量守恒」模拟：水泵运行时把水从储水槽送到加热槽，停机后两槽缓慢回平。
# 速率按"演示能看清、又不会几十秒就抽干/灌满"取值，现场可按需调整。
SIM_LEVEL_STORAGE_INITIAL = _env_float("WATER_SIM_LEVEL_STORAGE_INITIAL", 65.0)  # 储水槽初始 %
SIM_LEVEL_HEATER_INITIAL = _env_float("WATER_SIM_LEVEL_HEATER_INITIAL", 35.0)    # 加热槽初始 %
SIM_LEVEL_TRANSFER_PER_S = _env_float("WATER_SIM_LEVEL_TRANSFER", 0.35)   # 泵运行时转移速率 %/s
SIM_LEVEL_BALANCE_RATE = _env_float("WATER_SIM_LEVEL_BALANCE", 0.05)      # 停机后两槽回平速率(比例/s)
SIM_LEVEL_STORAGE_MIN = _env_float("WATER_SIM_LEVEL_STORAGE_MIN", 5.0)    # 储水槽最低保留 %（防抽干）
SIM_LEVEL_FLOW_REF = _env_float("WATER_SIM_LEVEL_FLOW_REF", 30.0)         # 视为"满速"的流量 L/min

# 告警阈值(可在运行期"系统配置"页修改，数据库 config 表覆盖此处默认值)
# 仅列出当前固件支持的通道；温度/压力阈值需对应通道启用后才会生效。
# 注意：flow_max 是"正常流量的上限"，请按现场实测最大流量留 1.5~2 倍余量。
#       现场实测本水循环回路约 21~56 L/min（波动很大，偶有尖峰），故默认取 80 避免持续误报；
#       如需演示告警功能，可临时把上限调到低于当前流量。
DEFAULT_THRESHOLDS = {
    "flow_max": 80.0,           # 【现场修改】流量上限 L/min
    "flow_min": 0.0,            # 【现场修改】流量下限 L/min
}

# 恒温闭环 PID（任务六）：需设备同时支持温度与加热通道，当前固件不支持，故默认不可用。
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

# ---------- 账号与令牌 ----------
JWT_SECRET = _env("IOT_JWT_SECRET", "iot-water-secret")
TOKEN_TTL = _env_int("IOT_TOKEN_TTL", 12 * 3600)
ADMIN_USERNAME = _env("IOT_ADMIN_USER", "admin")
ADMIN_DEFAULT_PASSWORD = _env("IOT_ADMIN_PASSWORD", "admin123")

# ---------- 路径 ----------
BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = Path(_env("IOT_FRONTEND_DIR", str(BASE_DIR.parent / "frontend")))


def pid_supported() -> bool:
    """恒温闭环需要温度采集 + 加热控制两个通道同时可用。"""
    return bool(DEVICE_FEATURES.get("temperature") and DEVICE_FEATURES.get("heater"))


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
