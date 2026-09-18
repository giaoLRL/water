"""部署自检（backend/preflight.py）的单元测试：不需要数据库/网络，可独立运行。

覆盖：
1. Python 版本判断（正常 / 版本过低）；
2. 依赖检查（齐全 / 缺包时的建议里带 pip 命令）；
3. 关键文件检查（前端目录存在；用 IOT_FRONTEND_DIR 指向不存在的目录时降级为提示）；
4. 端口检查（空闲=OK；被占用=fatal 时 FAIL、一键体检时 WARN）；
5. 数据库错误分类（2003/1045/1049/1044/1040 给出不同建议）；
6. 日志写入 .runtime/startup.log。

运行：python backend/tests/test_preflight.py
"""
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import preflight     # noqa: E402
import config        # noqa: E402

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")


print("== Python 版本判断 ==")
item = preflight.check_python()
check("当前解释器版本合格", item.level == preflight.OK, f"{item.detail} {item.hint}")
_saved = preflight.REQUIRED_PY
preflight.REQUIRED_PY = (99, 0)
low = preflight.check_python()
preflight.REQUIRED_PY = _saved
check("版本过低判为失败并给出建议", low.level == preflight.FAIL and "Python" in low.hint, low.hint)

print("== 依赖检查 ==")
deps = preflight.check_dependencies()
check("本机依赖齐全判为通过", len(deps) == 1 and deps[0].level == preflight.OK, str(deps[0].detail))
_saved_mods = preflight.REQUIRED_MODULES
preflight.REQUIRED_MODULES = _saved_mods + [("__not_exist_pkg__", "假依赖", "fake-pkg")]
missing = preflight.check_dependencies()
preflight.REQUIRED_MODULES = _saved_mods
check("缺包时判为失败且建议里带 pip install",
      missing[0].level == preflight.FAIL and "pip install" in missing[0].hint and "fake-pkg" in missing[0].hint,
      missing[0].hint)

print("== 关键文件检查（含前端目录）==")
files = preflight.check_files()
check("前端目录正常时通过",
      any(i.level == preflight.OK and i.title == "前端静态文件" for i in files),
      str([(i.level, i.title, i.detail) for i in files]))
_saved_front = os.environ.get("IOT_FRONTEND_DIR")
os.environ["IOT_FRONTEND_DIR"] = str(preflight.REPO_ROOT / "no_such_frontend_dir")
import importlib
importlib.reload(config)
files2 = preflight.check_files()
front2 = [i for i in files2 if i.title == "前端静态文件"]
check("前端目录缺失时降级为提示（不阻断接口）",
      bool(front2) and front2[0].level == preflight.WARN and "接口仍可用" in front2[0].hint,
      str(front2[0].detail if front2 else ""))
if _saved_front is None:
    os.environ.pop("IOT_FRONTEND_DIR", None)
else:
    os.environ["IOT_FRONTEND_DIR"] = _saved_front
importlib.reload(config)

print("== 端口检查 ==")
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.bind(("0.0.0.0", 0))      # 与 check_port 相同的绑定地址，才能真实复现"被占用"
srv.listen(1)
busy_port = srv.getsockname()[1]
busy = preflight.check_port(busy_port)
check("端口被占用：启动路径下判失败", busy.level == preflight.FAIL, busy.detail)
check("端口被占用的建议里给出结束进程/换端口两条路",
      "Stop-Process" in busy.hint and "WATER_PORT" in busy.hint, busy.hint)
soft = preflight.check_port(busy_port, fatal=False)
check("端口被占用：一键体检下降级为提示", soft.level == preflight.WARN, soft.detail)
srv.close()
free = ""
for p in range(18080, 18120):
    try:
        t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        t.bind(("0.0.0.0", p))
        t.close()
        free = p
        break
    except OSError:
        continue
check("空闲端口判为通过", preflight.check_port(int(free)).level == preflight.OK)

print("== 数据库错误分类（构造异常，不连真实数据库）==")
params = {"host": "127.0.0.1", "port": 3306, "user": "iot_user", "password": "x", "name": "iot_system"}
cases = {
    2003: "Get-Service MySQL80",
    1045: "CREATE USER IF NOT EXISTS",
    1049: "CREATE DATABASE",
    1044: "GRANT ALL PRIVILEGES",
    1040: "max_connections",
}
for code, keyword in cases.items():
    exc = Exception(code, "模拟错误")
    exc.args = (code, "模拟错误")
    detail, hint = preflight._classify_db_error(exc, params)
    check(f"错误码 {code} 的建议包含关键命令", keyword in hint, hint[:80])
unknown = Exception("奇怪的错误")
check("未知错误也有兜底建议", "按报错原文排查" in preflight._classify_db_error(unknown, params)[1])

print("== 日志写入 ==")
before = preflight.LOG_PATH.stat().st_size if preflight.LOG_PATH.exists() else 0
preflight.log("[TEST] 自检日志写入测试")
after = preflight.LOG_PATH.stat().st_size if preflight.LOG_PATH.exists() else 0
check("日志文件已追加写入 .runtime/startup.log", after > before, str(preflight.LOG_PATH))
check("日志内容可读回", "[TEST] 自检日志写入测试" in preflight.LOG_PATH.read_text(encoding="utf-8"))

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
