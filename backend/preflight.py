"""启动自检：部署前把"能不能跑起来"一次查完，并把结论写进日志文件。

设计约束（重要）：
1. **只依赖标准库**——否则"缺依赖"这种最常见的失败反而查不出来；
2. 任何一项失败都要**同时**打印到控制台并追加写入 `.runtime/startup.log`（带时间戳），
   也就是"部署失败必须留痕"，方便赛后复盘/交付；
3. 每条失败都带**可直接执行的修复建议**（命令级），比赛现场不需要重新读代码。

用法：
    python backend/preflight.py            # 一键体检（人来读的报告，退出码 0/2）
    由 main.py 在导入业务依赖之前自动调用（失败则终止启动并打印结论）。

检查项：Python 版本 / 第三方依赖 / 关键文件（含前端目录）/ 端口占用 / 数据库连接
        （按 2003、1045、1049 等错误码给出不同建议）/ 现场设备可达性（仅提示，不阻断）。
"""
from __future__ import annotations

import importlib.util
import os
import platform
import socket
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = REPO_ROOT / ".runtime"
LOG_PATH = RUNTIME_DIR / "startup.log"

OK, WARN, FAIL = "OK", "WARN", "FAIL"
REQUIRED_PY = (3, 11)
# 第三方依赖：(import 名, 说明, pip 包名)
REQUIRED_MODULES = [
    ("fastapi", "FastAPI 框架", "fastapi"),
    ("uvicorn", "ASGI 服务器", "uvicorn"),
    ("pymysql", "MySQL 驱动", "PyMySQL"),
    ("dbutils", "数据库连接池", "DBUtils"),
]


class Item:
    """一条检查结果：level ∈ {OK,WARN,FAIL}。"""

    __slots__ = ("level", "title", "detail", "hint")

    def __init__(self, level: str, title: str, detail: str = "", hint: str = "") -> None:
        self.level = level
        self.title = title
        self.detail = detail
        self.hint = hint


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(line: str = "", also_print: bool = True) -> None:
    """写一行日志：控制台 + .runtime/startup.log（日志目录不可写时只打印，不影响启动）。"""
    text = f"{_stamp()} | {line}" if line else _stamp()
    if also_print:
        try:
            print(text, flush=True)
        except OSError:      # 极端情况：stdout 被重定向到已关闭的管道
            pass
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass


def log_exception(title: str, exc: BaseException) -> None:
    """记录一个致命异常：标题 + 完整堆栈（堆栈只进日志文件，避免刷屏）。"""
    log(f"[FAIL] {title}: {type(exc).__name__}: {exc}")
    log("--- 详细堆栈（见日志文件）---", also_print=False)
    for line in traceback.format_exc().rstrip().splitlines():
        log("  " + line, also_print=False)


# =============================================================
# 各项检查
# =============================================================
def check_python() -> Item:
    ver = sys.version_info
    text = f"{ver.major}.{ver.minor}.{ver.micro} ({sys.executable})"
    if (ver.major, ver.minor) < REQUIRED_PY:
        return Item(FAIL, "Python 版本", text,
                    f"需要 Python {REQUIRED_PY[0]}.{REQUIRED_PY[1]} 或更高（当前 {ver.major}.{ver.minor}）。"
                    "请安装新版 Python 并用 `python -m venv .venv` 重建虚拟环境后重试。")
    return Item(OK, "Python 版本", text)


def check_venv() -> Item:
    """提示当前用的是哪个解释器（比赛现场最常见的问题是"装到了别处/没在 venv 里跑"）。"""
    inside = Path(sys.prefix).resolve() == (REPO_ROOT / ".venv").resolve()
    venv_dir = REPO_ROOT / ".venv"
    if inside:
        return Item(OK, "虚拟环境", f"正在使用项目虚拟环境 {venv_dir}")
    if venv_dir.exists():
        return Item(WARN, "虚拟环境", f"当前解释器不是项目虚拟环境（{sys.executable}）",
                    "项目虚拟环境已存在，建议用它启动："
                    r'& "..\.venv\Scripts\python.exe" main.py'
                    "（在 backend 目录下执行；用别的解释器可能缺依赖）")
    return Item(WARN, "虚拟环境", f"未找到 {venv_dir}（当前解释器 {sys.executable}）",
                r'先在仓库根目录创建：python -m venv .venv ；'
                r'再装依赖：& ".\.venv\Scripts\python.exe" -m pip install -r backend\requirements.txt')


def check_dependencies() -> list[Item]:
    items: list[Item] = []
    missing: list[tuple[str, str]] = []
    for mod, desc, pkg in REQUIRED_MODULES:
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append((pkg, desc))
    if not missing:
        return [Item(OK, "第三方依赖", " / ".join(m[1] for m in REQUIRED_MODULES) + " 均已安装")]
    pkgs = " ".join(p for p, _ in missing)
    detail = "缺少：" + "、".join(f"{desc}(pip 包 {pkg})" for pkg, desc in missing)
    hint = ("联网时：& \".\\.venv\\Scripts\\python.exe\" -m pip install -r backend\\requirements.txt\n"
            f"     只补缺的包：& \".\\.venv\\Scripts\\python.exe\" -m pip install {pkgs}\n"
            "     无网络时见《从零部署手册》\"离线安装依赖\"一节"
            r"（先在有网机器上 pip download 出 wheel，再 pip install --no-index --find-links 安装）")
    items.append(Item(FAIL, "第三方依赖", detail, hint))
    return items


def check_files() -> list[Item]:
    """关键文件/目录是否齐全：后端必需文件 + 前端静态目录（缺前端只会白屏，不阻断接口）。"""
    items: list[Item] = []
    need_backend = ["main.py", "config.py", "api.py", "database.py", "requirements.txt"]
    missing = [f for f in need_backend if not (BACKEND_DIR / f).exists()]
    if missing:
        items.append(Item(FAIL, "后端文件", f"缺少 {', '.join(missing)}（目录 {BACKEND_DIR}）",
                          "项目文件不完整：重新解压/克隆仓库，确认 backend 目录下有全部 .py 文件。"))
    else:
        items.append(Item(OK, "后端文件", f"{BACKEND_DIR} 必需文件齐全"))

    try:
        import config  # 仅用于取配置里的前端目录；失败另行提示
        front = Path(config.FRONTEND_DIR)
    except Exception as exc:  # noqa: BLE001
        items.append(Item(FAIL, "配置读取", f"无法导入 backend/config.py：{type(exc).__name__}: {exc}",
                          "检查 config.py 是否被改坏（语法错误/非法环境变量），或用 git 还原该文件。"))
        return items

    index = front / "index.html"
    app_js = front / "js" / "app.js"
    if not front.exists():
        items.append(Item(WARN, "前端静态文件", f"目录不存在：{front}",
                          "接口仍可用（/docs 可访问），但网页会 404。请确认 frontend 目录存在，"
                          "或在 backend/config.py 里改 FRONTEND_DIR。"))
    elif not (index.exists() and app_js.exists()):
        items.append(Item(WARN, "前端静态文件", f"目录存在但缺少 index.html 或 js/app.js：{front}",
                          "前端文件不完整：重新解压/克隆仓库的 frontend 目录。"))
    else:
        items.append(Item(OK, "前端静态文件", f"{front}（含 index.html / js/app.js）"))
    return items


def _port_owner(port: int) -> str:
    """尽力查出占用端口的 PID（失败返回空串，不影响主流程）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             timeout=5, encoding="utf-8", errors="ignore").stdout
    except Exception:  # noqa: BLE001
        return ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].endswith(f":{port}"):
            return f"占用进程 PID={parts[4]}"
    return ""


def check_port(port: int, fatal: bool = True) -> Item:
    """端口能否绑定。fatal=False（一键体检）时，被占用只算提示——服务可能本来就在跑。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as exc:
        owner = _port_owner(port)
        detail = f"无法绑定：{exc} {owner}".strip()
        hint = (f"若服务已在运行属正常（先确认：curl http://127.0.0.1:{port}/docs 是否 200）。\n"
                f"     确实要重启时结束占用进程："
                f"Stop-Process -Id (Get-NetTCPConnection -LocalPort {port} -State Listen).OwningProcess -Force\n"
                f"     或换端口启动：python main.py 8010（或先设置环境变量 WATER_PORT=8010）")
        return Item(FAIL if fatal else WARN, f"端口 {port}", detail, hint)
    finally:
        sock.close()
    return Item(OK, f"端口 {port}", "可绑定，未被占用")


def _db_params() -> dict:
    import config
    return {"host": config.DB_HOST, "port": int(config.DB_PORT), "user": config.DB_USER,
            "password": config.DB_PASSWORD, "name": config.DB_NAME}


def _classify_db_error(exc: BaseException, p: dict) -> tuple[str, str]:
    """把数据库异常翻译成"原因 + 可执行建议"。"""
    code = getattr(exc, "args", [None])[0]
    text = str(exc)
    where = f"{p['user']}@{p['host']}:{p['port']}"
    if code == 2003 or "10061" in text or "Can't connect" in text:
        return (f"连接不上 MySQL（{where}）：{text}",
                "① 数据库服务没启动：`Get-Service MySQL80` 看状态，`Start-Service MySQL80` 启动；\n"
                "     ② 地址/端口不对：确认 IOT_DB_HOST / IOT_DB_PORT（默认 127.0.0.1:3306）；\n"
                "     ③ 装了别的 MySQL 或改过端口，就改 backend/config.py 或设环境变量")
    if code == 1045:
        return (f"账号或密码错误（{where}）：{text}",
                "确认 backend/config.py 里的 DB_USER / DB_PASSWORD，或环境变量 IOT_DB_USER / IOT_DB_PASSWORD。\n"
                "     还没建账号时用 root 执行：\n"
                "     CREATE USER IF NOT EXISTS 'iot_user'@'127.0.0.1' IDENTIFIED BY 'iot_pass_2026';\n"
                f"     CREATE DATABASE IF NOT EXISTS {p['name']} DEFAULT CHARACTER SET utf8mb4;\n"
                f"     GRANT ALL PRIVILEGES ON {p['name']}.* TO 'iot_user'@'127.0.0.1'; FLUSH PRIVILEGES;")
    if code == 1049:
        return (f"数据库不存在（{p['name']}）：{text}",
                f"用 root 建库：CREATE DATABASE {p['name']} DEFAULT CHARACTER SET utf8mb4;"
                "（正常情况下后端会自动建库，若报这个错说明当前账号没有建库权限）")
    if code == 1044:
        return (f"账号无权访问库 {p['name']}：{text}",
                f"用 root 授权：GRANT ALL PRIVILEGES ON {p['name']}.* TO '{p['user']}'@'127.0.0.1'; FLUSH PRIVILEGES;")
    if code == 1040:
        return (f"MySQL 连接数已满：{text}", "减少并发或调大 max_connections；也可把 IOT_DB_POOL_SIZE 调小。")
    return (f"{type(exc).__name__}: {text}",
            "按报错原文排查；确认 MySQL 已启动、库与账号已创建（详见《从零部署手册》第 4 步）。")


def check_database() -> list[Item]:
    """连一次数据库（不指定库，和启动时建库用的方式一致）并检查库是否存在。"""
    try:
        import pymysql
    except ImportError:
        return [Item(WARN, "数据库连接", "跳过：pymysql 未安装（先解决上面的依赖问题）")]
    try:
        p = _db_params()
    except Exception as exc:  # noqa: BLE001
        return [Item(FAIL, "数据库配置", f"读取 config.py 失败：{type(exc).__name__}: {exc}",
                     "检查 backend/config.py 或相关环境变量（IOT_DB_*）。")]
    items: list[Item] = []
    try:
        conn = pymysql.connect(host=p["host"], port=p["port"], user=p["user"],
                               password=p["password"], charset="utf8mb4", connect_timeout=5,
                               autocommit=True)
    except Exception as exc:  # noqa: BLE001
        detail, hint = _classify_db_error(exc, p)
        return [Item(FAIL, "数据库连接", detail, hint)]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
            cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s", (p["name"],))
            exists = cur.fetchone() is not None
        items.append(Item(OK, "数据库连接", f"MySQL {version} @ {p['host']}:{p['port']}，账号 {p['user']} 可用"))
        if exists:
            items.append(Item(OK, "数据库", f"{p['name']} 已存在（表结构会在启动时自动建/迁移）"))
        else:
            items.append(Item(WARN, "数据库", f"{p['name']} 还不存在（启动时会自动创建）",
                              "若启动时报 1044/1049，用 root 建库并授权（见上一条建议）。"))
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return items


def check_device() -> Item:
    """现场 ESP32 是否可达：**不阻断启动**（离线是设计内状态，界面会显示"离线"）。"""
    try:
        import config
        from urllib.request import urlopen
    except Exception as exc:  # noqa: BLE001
        return Item(WARN, "现场设备", f"跳过：{type(exc).__name__}: {exc}")
    url = config.DEVICE_URL.rstrip("/") + "/api/health"
    try:
        with urlopen(url, timeout=min(float(config.DEVICE_TIMEOUT_S), 2.0)) as resp:
            body = resp.read(200).decode("utf-8", "ignore").replace("\n", " ")[:120]
        return Item(OK, "现场设备", f"{config.DEVICE_URL} 可达（{body}）")
    except Exception as exc:  # noqa: BLE001
        return Item(WARN, "现场设备", f"{config.DEVICE_URL} 当前不可达（{type(exc).__name__}: {exc}）",
                    "不影响后端启动：界面会显示「离线」、读数按真实模式给 --/0。"
                    "现场请确认设备已上电、与本机同网段（见《部署指南》7.6）。")


# =============================================================
# 汇总执行
# =============================================================
def run_checks(port: int | None = None, device: bool = True, port_fatal: bool = True) -> list[Item]:
    items: list[Item] = [check_python(), check_venv()]
    items += check_dependencies()
    items += check_files()
    if port:
        items.append(check_port(port, fatal=port_fatal))
    items += check_database()
    if device:
        items.append(check_device())
    return items


def summarize(items: list[Item]) -> tuple[bool, str]:
    ok = sum(1 for i in items if i.level == OK)
    warn = sum(1 for i in items if i.level == WARN)
    fail = sum(1 for i in items if i.level == FAIL)
    if fail:
        return False, f"自检未通过（{ok} 项正常 / {warn} 项提示 / {fail} 项失败）"
    return True, f"自检通过（{ok} 项正常 / {warn} 项提示 / 0 项失败）"


def report(items: list[Item], started: bool = True) -> bool:
    """打印 + 写日志，返回是否通过。"""
    icon = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}
    log("=" * 72)
    log(("启动自检" if started else "一键体检") + f"  |  主机 {platform.node()}  |  Python {platform.python_version()}")
    for it in items:
        line = f"{icon[it.level]} {it.title:<14} {it.detail}"
        log(line)
        if it.level != OK and it.hint:
            for hint_line in str(it.hint).splitlines():
                log("        ↳ " + hint_line)
    passed, summary = summarize(items)
    log("结论：" + summary)
    if not passed:
        log("启动已终止：请按上面的「↳ 处理建议」修复后重试；本次记录已写入 "
            f"{LOG_PATH}")
    log("=" * 72)
    return passed


def abort(items: list[Item]):
    """启动前的致命问题：打印结论并退出（退出码 2，便于脚本判断）。"""
    log("!!! 无法启动：部署自检未通过 !!!")
    report(items)
    sys.exit(2)


def main() -> int:
    """`python backend/preflight.py` 一键体检入口。"""
    port = int(os.environ.get("WATER_PORT", "8000"))
    items = run_checks(port=port, port_fatal=False)
    ok = report(items, started=False)
    print(f"\n日志文件：{LOG_PATH}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
