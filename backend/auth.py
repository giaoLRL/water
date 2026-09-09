"""账号与权限：PBKDF2 密码哈希、自签名 HS256 JWT、权限点定义、角色矩阵鉴权。

- 密码哈希：hashlib.pbkdf2_hmac（标准库实现，无第三方依赖）
- 令牌：自签名 HS256 JWT（hmac + base64url 手写实现，无第三方依赖）
- 权限点：每个功能一个 key，角色由一串权限点组成；admin 角色拥有全部权限（*）
- 角色矩阵：持久化到数据库 config 表（sys.roles），"系统配置 → 账号与权限"可勾选修改
- 视频流等 <img> 直连接口的 token 通过 query 参数传递，其余接口走 Authorization 头

未登录返回 40101，已登录无权限返回 40301（统一由 main.py 异常处理器转换）。
"""
import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import Depends, Request
from fastapi.exceptions import HTTPException

import config
import database
import store

# 令牌有效期（秒）
TOKEN_TTL = config.TOKEN_TTL

# 权限点定义：(key, 显示名)。顺序即矩阵列顺序
PERMISSIONS = [
    ("view_monitor", "实时监控"),
    ("view_history", "历史数据"),
    ("view_detect", "人员监测"),
    ("view_alarm", "告警记录"),
    ("view_log", "操作日志"),
    ("view_device", "设备状态"),
    ("ctrl_light", "设备控制"),
    ("cfg_alarm", "告警阈值"),
    ("cfg_system", "系统配置"),
    ("account_manage", "账号管理"),
]

# 预置角色：(key, 显示名, 权限点列表)；admin 用 "*" 表示全权限
DEFAULT_ROLES = {
    "admin": ("管理员", "*"),
    "operator": ("运维", ["view_monitor", "view_history", "view_detect", "view_alarm",
                          "view_log", "view_device", "ctrl_light", "cfg_alarm", "cfg_system"]),
    "viewer": ("观察员", ["view_monitor", "view_history", "view_detect", "view_alarm",
                          "view_log", "view_device"]),
}


# ---------- 角色矩阵 ----------
def role_matrix() -> dict:
    """当前角色矩阵 {role_key: (显示名, 权限列表或"*")}，回退代码默认。"""
    raw = store.get_json("roles", None)
    if raw is None:
        return {k: list(v) for k, v in DEFAULT_ROLES.items()}
    return raw


def save_role_matrix(matrix: dict) -> None:
    store.set_json("roles", matrix)


def perms_of(role: str) -> list[str]:
    """某角色拥有的权限点；admin（或矩阵值为 "*"）返回全部权限。"""
    matrix = role_matrix()
    item = matrix.get(role)
    if item is None:
        return []
    perms = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else item
    if perms == "*":
        return [p[0] for p in PERMISSIONS]
    return list(perms)


def role_label(role: str) -> str:
    matrix = role_matrix()
    item = matrix.get(role)
    if isinstance(item, (list, tuple)) and item:
        return str(item[0])
    return role


# ---------- 密码哈希（PBKDF2-HMAC-SHA256） ----------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return "pbkdf2$%s$%s" % (base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        dk = base64.b64decode(dk_b64)
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
        return hmac.compare_digest(calc, dk)
    except Exception:  # noqa: BLE001
        return False


# ---------- JWT（HS256） ----------
def jwt_secret() -> str:
    """读取或生成持久化的签名密钥（重启不失效）。"""
    s = store.get("jwt_secret", None)
    if not s or len(s) < 16:
        s = secrets.token_hex(32)
        store.set("jwt_secret", s)
    return s


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(username: str, role: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": username,
        "role": role,
        "iat": int(time.time()),
        "exp": int(time.time()) + TOKEN_TTL,
    }
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(jwt_secret().encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"


def parse_token(token: str | None) -> dict | None:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header, payload, sig = parts
    expect = hmac.new(jwt_secret().encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_unb64url(sig), expect):
        return None
    try:
        data = json.loads(_unb64url(payload))
    except Exception:  # noqa: BLE001
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


# ---------- FastAPI 鉴权依赖 ----------
def get_current_user(request: Request) -> dict:
    """从 Authorization 头或 query token 取令牌；失败抛 401。"""
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else None
    if not token:
        token = request.query_params.get("token")
    data = parse_token(token)
    if data is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    role = data.get("role", "")
    return {
        "username": data.get("sub", ""),
        "role": role,
        "perms": perms_of(role),
    }


def require_perm(perm: str):
    """接口权限校验依赖工厂：用户缺少该权限时抛 403。"""
    def dep(user: dict = Depends(get_current_user)):
        if perm not in user["perms"]:
            raise HTTPException(status_code=403, detail=f"缺少权限: {perm}")
        return user
    return dep


# ---------- 账号初始化 ----------
def ensure_admin() -> None:
    """数据库首次建表后创建默认管理员 admin（默认密码 admin123）。"""
    if database.count_users() > 0:
        return
    database.insert_user(
        username=config.ADMIN_USERNAME,
        password_hash=hash_password(config.ADMIN_DEFAULT_PASSWORD),
        role="admin",
    )