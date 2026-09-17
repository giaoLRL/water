"""全局配置：现场采集设备(ESP32)、数据库、采集周期、告警阈值、判定服务、账号。

所有比赛现场可能需要修改的参数都集中在本文件，用【现场修改】标注。
各业务模块统一 import config 读取；可被 IOT_* / WATER_* 环境变量覆盖。

数据来源（纯真实模式）：
    全部传感/执行数据均来自现场 ESP32 采集端（默认 http://192.168.31.100），
    后端不生成任何模拟数据；设备离线时相关字段返回 None（水位类恒为 0），前端提示“设备离线”。
    设备能力由 DEVICE_FEATURES 声明，未支持的通道一律不采集、不告警、不展示。
"""
import os
from pathlib import Path
from urllib.parse import urlparse


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

# 设备能力表（对应现场固件 web_server.cpp 实际提供的通道，接口全部为 GET）。
# 现场固件提供：流量 / 累计水量 / 水泵 / 定量目标 / 清零 / 水温×2 / 压力 / 加热继电器 /
#               超声波水位(单路，接加热槽) / 光照(GY-302)。
# 【现场修改】某通道固件未烧录时把对应项改为 False：
# 采集、告警、接口与前端会自动裁剪该通道，无需改动其他代码。
#
# level_* = 液位通道：双水槽循环加热系统。固件只提供一路 /api/level（超声波，接加热槽），
#   故 level_heater=True 读实测值；储水槽无传感器，level_storage=False。
#   True  → 从对应 LEVEL_PATH_* 读取真实液位；
#   False → 该槽无数据来源，水位恒为 0（界面标注「无传感器」），不做任何模拟。
# light = 光照(GY-302, lx)：/api/light，传感器未识别时固件返回 503（该通道无数据）。
DEVICE_FEATURES = {
    "flow": True,
    "volume": True,
    "pump": True,
    "pump_target": True,
    "volume_reset": True,
    "level_storage": False,   # 储水槽液位：固件无第二路传感器 → 恒为 0，界面标注「无传感器」
    "level_heater": True,     # 加热槽液位：超声波传感器 /api/level（返回百分比）
    "temperature": True,    # 水温×2：/api/temperature(探头1→储水槽)、/api/temperature2(探头2→加热槽)
    "pressure": True,       # 水压：/api/pressure，固件返回 MPa，后端统一换算为 kPa
    "heater": True,         # 加热继电器：/api/heater/on|off|toggle|state
    "light": True,          # 光照：/api/light（GY-302，单位 lx）
}

# 仪表盘自定义卡片代理白名单（仅主机名）：前端「自定义接口卡片」的 URL
# 由后端 /api/water/dashboard/proxy 代发 GET，仅允许白名单内主机，防 SSRF。
# 【现场修改】需要接入其他设备/服务时把主机名加进 WATER_DASHBOARD_PROXY_HOSTS（逗号分隔）。
_proxy_extra = [h.strip() for h in _env("WATER_DASHBOARD_PROXY_HOSTS", "").split(",") if h.strip()]
DASHBOARD_PROXY_HOSTS = sorted(
    {"127.0.0.1", "localhost", urlparse(DEVICE_URL).hostname or "", *_proxy_extra} - {""}
)

# ---------- 双水槽液位通道 ----------
# 固件只提供一路超声波液位 /api/level（接加热槽，返回百分比），储水槽无传感器。
# LEVEL_PATH 置空字符串表示该槽无传感器路径（water_device 会跳过读取）。
TANKS = ("storage", "heater")
TANK_LABELS = {"storage": "储水槽", "heater": "加热槽"}

LEVEL_PATH_STORAGE = _env("WATER_LEVEL_PATH_STORAGE", "")                     # 储水槽无传感器，留空
LEVEL_PATH_HEATER = _env("WATER_LEVEL_PATH_HEATER", "/api/level")             # 超声波液位（百分比）
# /api/level/height?value=mm 为超声波"预定高度"标定(NVS 掉电不丢)，由固件侧换算百分比，后端无需调用
TANK_HEIGHT_CM_STORAGE = _env_float("WATER_TANK_HEIGHT_CM_STORAGE", 100.0)    # 【现场修改】水槽高度(cm)
TANK_HEIGHT_CM_HEATER = _env_float("WATER_TANK_HEIGHT_CM_HEATER", 100.0)      # 【现场修改】水槽高度(cm)

# 水槽容积(L)：把液位百分比换算成"估算水量"显示用；按现场水槽实际容积填写。
TANK_CAPACITY_STORAGE_DEFAULT = _env_float("WATER_TANK_CAPACITY_STORAGE", 1000.0)   # 【现场修改】
TANK_CAPACITY_HEATER_DEFAULT = _env_float("WATER_TANK_CAPACITY_HEATER", 1000.0)     # 【现场修改】

# 告警阈值(可在运行期"系统配置"页修改，数据库 config 表覆盖此处默认值)。
# 注意：压力阈值单位为 kPa，设备上报是 MPa，后端已按 1 MPa = 1000 kPa 统一换算。
#       flow_max 是"正常流量的上限"，请按现场实测最大流量留 1.5~2 倍余量；
#       现场实测本水循环回路约 21~56 L/min（波动很大，偶有尖峰），故默认取 80 避免持续误报；
#       如需演示告警功能，可临时把上限调到低于当前流量。
DEFAULT_THRESHOLDS = {
    "storage_temp_max": 45.0,   # 【现场修改】储水槽温度上限℃
    "storage_temp_min": 0.0,    # 【现场修改】储水槽温度下限℃
    "heater_temp_max": 60.0,    # 【现场修改】加热槽温度上限℃
    "heater_temp_min": 0.0,     # 【现场修改】加热槽温度下限℃
    "flow_max": 80.0,           # 【现场修改】流量上限 L/min
    "flow_min": 0.0,            # 【现场修改】流量下限 L/min
    "pressure_max": 400.0,      # 【现场修改】压力上限 kPa（固件文档示例读数约 0.123 MPa ≈ 123 kPa）
    "pressure_min": 10.0,       # 【现场修改】压力下限 kPa
    "light_max": 2000.0,        # 【现场修改】光照上限 lx（室内照明约 100~1000，留余量防误报）
    "light_min": 0.0,           # 【现场修改】光照下限 lx
}

# 恒温闭环 PID（任务六）：需温度采集 + 加热控制两个通道同时可用，当前固件均已提供。
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
