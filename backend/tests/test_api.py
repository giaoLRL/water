"""水循环后端接口功能测试：账号/权限、实时、历史、控制、PID、统计、告警、日志。
用法: python backend/tests/test_api.py [base_url]（缺省 http://127.0.0.1:8000）
使用默认管理员 admin/admin123 登录后访问全部业务接口。
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ADMIN = ("admin", "admin123")

passed = 0
failed = 0
TOKEN = ""


def request_raw(method: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def request(method: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
    return request_raw(method, path, body, token or TOKEN)


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")


def now_fmt(offset_minutes: int = 0) -> str:
    return (datetime.now() + timedelta(minutes=offset_minutes)).strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    global TOKEN
    print("== 0. 账号与权限 ==")
    r = request_raw("GET", "/api/water/realtime")
    check("未登录访问返回 40101", r["code"] == 40101, str(r))
    r = request_raw("POST", "/api/auth/login", {"username": "admin", "password": "wrong"})
    check("错误密码登录失败", r["code"] == 40006, str(r))
    r = request_raw("POST", "/api/auth/login", {"username": ADMIN[0], "password": ADMIN[1]})
    check("admin 登录成功", r["code"] == 0 and "token" in r["data"], str(r))
    TOKEN = r["data"]["token"]
    check("admin 拥有全权限", "account_manage" in r["data"]["user"]["perms"], str(r["data"]["user"]["perms"]))
    r = request("GET", "/api/auth/me")
    check("auth/me 返回当前用户", r["code"] == 0 and r["data"]["username"] == "admin", str(r))

    test_user = f"tester{int(time.time())}"
    r = request("POST", "/api/auth/users", {"username": test_user, "password": "t123456", "role": "viewer"})
    check("创建用户成功", r["code"] == 0, str(r))

    print("== 0.1 权限拦截（viewer 无 ctrl_light） ==")
    r = request_raw("POST", "/api/auth/login", {"username": test_user, "password": "t123456"})
    viewer_token = r["data"]["token"]
    r = request_raw("GET", "/api/water/realtime", token=viewer_token)
    check("viewer 可看实时(view_monitor)", r["code"] == 0, str(r))
    r = request_raw("POST", "/api/water/pump", {"action": "on"}, token=viewer_token)
    check("viewer 控制水泵被拒 40301", r["code"] == 40301, str(r))
    r = request_raw("GET", "/api/auth/users", token=viewer_token)
    check("viewer 访问账号管理被拒 40301", r["code"] == 40301, str(r))

    print("== 1. 实时数据 ==")
    rt = request("GET", "/api/water/realtime")
    check("realtime 返回 code=0", rt["code"] == 0, str(rt))
    d = rt["data"]
    for f in ("storage_temp", "heater_temp", "flow_rate", "pressure",
              "pump_state", "heater_state", "total_flow", "active_alarms"):
        check(f"realtime 字段 {f}", f in d, str(d.keys()))
    check("温度有效(储水槽0~60)", 0 <= d["storage_temp"] <= 60, str(d["storage_temp"]))
    check("流量非负", d["flow_rate"] >= 0, str(d["flow_rate"]))

    print("== 2. 执行器控制 ==")
    r = request("POST", "/api/water/pump", {"action": "on"})
    check("开水泵成功", r["code"] == 0 and r["data"]["pump_state"] == "on", str(r))
    r = request("POST", "/api/water/pump", {"action": "off"})
    check("关水泵成功", r["code"] == 0 and r["data"]["pump_state"] == "off", str(r))
    r = request("POST", "/api/water/heater", {"action": "on"})
    check("开加热成功", r["code"] == 0 and r["data"]["heater_state"] == "on", str(r))
    r = request("POST", "/api/water/heater", {"action": "off"})
    check("关加热成功", r["code"] == 0 and r["data"]["heater_state"] == "off", str(r))
    r = request("POST", "/api/water/pump", {"action": "invalid"})
    check("非法指令返回 40002", r["code"] == 40002, str(r))

    print("== 3. 恒温PID ==")
    r = request("GET", "/api/water/pid")
    check("PID参数读取", r["code"] == 0 and "target_temp" in r["data"], str(r))
    r = request("POST", "/api/water/target", {"temp": 40.0})
    check("设定目标温度", r["code"] == 0 and r["data"]["target_temp"] == 40.0, str(r))
    r = request("POST", "/api/water/pid/mode", {"enabled": 1})
    check("开启恒温PID", r["code"] == 0 and r["data"]["pid_enabled"], str(r))
    time.sleep(3)
    rt = request("GET", "/api/water/realtime")["data"]
    check("恒温开启后realtime含占空比", "pid_duty" in rt, str(rt.keys()))
    request("POST", "/api/water/pid/mode", {"enabled": 0})

    print("== 4. 历史数据 ==")
    start, end = now_fmt(-10), now_fmt(1)
    r = request("GET", f"/api/water/history?start={urllib.parse.quote(start)}&end={urllib.parse.quote(end)}")
    check("历史查询返回点", r["code"] == 0 and isinstance(r["data"]["points"], list), str(r))
    check("历史点含4项传感+2状态", len(r["data"]["points"]) == 0 or all(
        k in r["data"]["points"][0] for k in ("storage_temp", "heater_temp", "flow_rate",
                                             "pressure", "pump_state", "heater_state")
    ), str(r))
    r = request("GET", "/api/water/history")
    check("缺少参数返回 40002", r["code"] == 40002, str(r))

    print("== 5. 数据统计 ==")
    r = request("GET", f"/api/water/stats?start={urllib.parse.quote(start)}&end={urllib.parse.quote(end)}")
    check("统计返回平均温度/压力/累计流量", r["code"] == 0 and "avg_heater_temp" in r["data"]
          and "max_pressure" in r["data"] and "total_flow" in r["data"], str(r))

    print("== 6. 告警 ==")
    r = request("GET", "/api/water/alarm/config")
    check("读取阈值成功", r["code"] == 0 and "storage_temp_max" in r["data"]["thresholds"], str(r))
    r = request("POST", "/api/water/alarm/config", {"pressure_max": 120.0})
    check("保存阈值成功", r["code"] == 0 and r["data"]["thresholds"]["pressure_max"] == 120.0, str(r))
    r = request("GET", "/api/water/alarms")
    check("告警日志可查询", r["code"] == 0 and isinstance(r["data"]["items"], list), str(r))
    r = request("GET", "/api/water/alarm/stats")
    check("告警统计可查询", r["code"] == 0 and isinstance(r["data"]["stats"], list), str(r))

    print("== 7. 操作日志与判定/系统状态 ==")
    r = request("GET", "/api/water/logs")
    check("操作日志返回", r["code"] == 0 and isinstance(r["data"]["items"], list), str(r))
    r = request("GET", "/api/water/system")
    check("系统状态返回", r["code"] == 0 and "server_time" in r["data"], str(r))
    r = request("GET", "/api/water/judge/status")
    check("判定服务状态可读", r["code"] == 0 and "enabled" in r["data"], str(r))

    print("== 8. 账号清理 ==")
    me = request("GET", "/api/auth/users")
    tid = next((u["id"] for u in me["data"]["users"] if u["username"] == test_user), None)
    r = request("DELETE", f"/api/auth/users/{tid}")
    check("删除测试用户", r["code"] == 0, str(r))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()