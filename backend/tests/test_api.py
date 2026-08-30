"""后端接口功能测试：覆盖账号/权限、灯杆实时/详情/历史/统计/控制/告警/人员监测/系统状态/日志。

用法: python backend/tests/test_api.py [base_url]（缺省 http://127.0.0.1:8000）
说明: 使用默认管理员 admin/admin123 登录后对所有业务接口鉴权访问。
"""
import json
import sys
import time
import urllib.error
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
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def request(method: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
    """带当前登录 token 的请求。"""
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
    # 未登录访问被拒
    r = request_raw("GET", "/api/lampposts")
    check("未登录访问返回 40101", r["code"] == 40101, str(r))
    # 错误密码
    r = request_raw("POST", "/api/auth/login", {"username": "admin", "password": "wrong"})
    check("错误密码登录失败", r["code"] == 40006, str(r))
    # 管理员登录
    r = request_raw("POST", "/api/auth/login", {"username": ADMIN[0], "password": ADMIN[1]})
    check("admin 登录成功", r["code"] == 0 and "token" in r["data"], str(r))
    TOKEN = r["data"]["token"]
    check("admin 拥有全权限", "account_manage" in r["data"]["user"]["perms"], str(r["data"]["user"]["perms"]))
    # 当前用户
    r = request("GET", "/api/auth/me")
    check("auth/me 返回当前用户", r["code"] == 0 and r["data"]["username"] == "admin", str(r))
    # 用户管理
    test_user = f"tester{int(time.time())}"
    r = request("POST", "/api/auth/users", {"username": test_user, "password": "t123456", "role": "viewer"})
    check("创建用户成功", r["code"] == 0, str(r))
    r = request("POST", "/api/auth/users", {"username": ADMIN[0], "password": "x", "role": "viewer"})
    check("重复用户名被拒", r["code"] == 40002, str(r))
    r = request("GET", "/api/auth/users")
    users = r["data"]["users"]
    check("用户列表不含密码哈希", all("password_hash" not in u for u in users), str(users[:1]))
    # 权限矩阵
    r = request("GET", "/api/auth/roles")
    check("角色矩阵可读", r["code"] == 0 and "admin" in r["data"]["roles"], str(r))

    print("== 0.1 权限拦截（viewer 无 ctrl_light） ==")
    r = request_raw("POST", "/api/auth/login", {"username": test_user, "password": "t123456"})
    viewer_token = r["data"]["token"]
    r = request_raw("GET", "/api/lampposts", token=viewer_token)
    check("viewer 可看监控(view_monitor)", r["code"] == 0, str(r))
    r = request_raw("POST", f"/api/lampposts/{r['data']['lampposts'][0]['id']}/control",
                    {"action": "on"}, token=viewer_token)
    check("viewer 开灯被拒 40301", r["code"] == 40301, str(r))
    r = request_raw("GET", "/api/sysconfig", token=viewer_token)
    check("viewer 访问配置被拒 40301", r["code"] == 40301, str(r))
    r = request_raw("GET", "/api/auth/users", token=viewer_token)
    check("viewer 访问账号管理被拒 40301", r["code"] == 40301, str(r))

    print("== 1. 灯杆列表与详情 ==")
    rt = request("GET", "/api/lampposts")
    check("lampposts 返回 code=0", rt["code"] == 0, str(rt))
    lamps = rt["data"]["lampposts"]
    check("包含至少 3 个灯杆", len(lamps) >= 3, "count=" + str(len(lamps)))
    fields = ("id", "name", "location", "temperature", "humidity", "luminance", "light_state")
    check("灯杆摘要字段完整", all(k in lamps[0] for k in fields), str(lamps[0].keys()))
    lamp_id = lamps[0]["id"]
    r = request("GET", f"/api/lampposts/{lamp_id}")
    check("灯杆详情返回", r["code"] == 0 and r["data"]["id"] == lamp_id, str(r))

    print("== 2. 环境参数实时数据 ==")
    check("温度数值有效", 0 < lamps[0]["temperature"] < 60, str(lamps[0]["temperature"]))
    check("湿度 0~100", 0 <= lamps[0]["humidity"] <= 100, str(lamps[0]["humidity"]))
    check("光照为非负", lamps[0]["luminance"] >= 0, str(lamps[0]["luminance"]))

    print("== 3. 设备控制（灯光） ==")
    r = request("POST", f"/api/lampposts/{lamp_id}/control", {"action": "on"})
    check("开灯成功", r["code"] == 0 and r["data"]["light_state"] == "on", str(r))
    r = request("POST", f"/api/lampposts/{lamp_id}/control", {"action": "off"})
    check("关灯成功", r["code"] == 0 and r["data"]["light_state"] == "off", str(r))
    r = request("POST", f"/api/lampposts/{lamp_id}/control", {"action": "invalid"})
    check("非法指令返回 40002", r["code"] == 40002, str(r))

    print("== 4. 历史数据 ==")
    start, end = now_fmt(-10), now_fmt(1)
    r = request("GET", f"/api/lampposts/{lamp_id}/history?start={urllib.parse.quote(start)}&end={urllib.parse.quote(end)}")
    check("历史查询返回点数据", r["code"] == 0 and isinstance(r["data"]["points"], list), str(r))
    check("历史点含传感字段", len(r["data"]["points"]) == 0 or all(
        k in r["data"]["points"][0] for k in ("ts", "temperature", "humidity", "luminance")
    ), str(r))
    r = request("GET", f"/api/lampposts/{lamp_id}/history")
    check("缺少参数返回 40002", r["code"] == 40002, str(r))

    print("== 5. 数据统计 ==")
    r = request("GET", f"/api/lampposts/{lamp_id}/stats?start={urllib.parse.quote(start)}&end={urllib.parse.quote(end)}")
    check("统计指标返回", r["code"] == 0 and "avg_temp" in r["data"], str(r))

    print("== 6. 截图与人员监测 ==")
    r = request("GET", f"/api/lampposts/{lamp_id}/snapshot")
    check("截图返回 base64 图片", r["code"] == 0 and str(r["data"]["image"]).startswith("data:image/"), str(r))
    time.sleep(1)
    r = request("POST", f"/api/lampposts/{lamp_id}/detect")
    if r["code"] == 0:
        d = r["data"]
        check("人员监测返回结果", "person_count" in d and "original_image" in d and "processed_image" in d, str(d.keys()))
        check("检测记录已入库", "id" in d and d["id"] > 0, str(d))
    else:
        print(f"  [SKIP] 人员监测（识别服务不可用: {r['msg']}）")

    print("== 7. 人员监测记录 ==")
    r = request("GET", f"/api/detections?lamp_id={lamp_id}")
    check("监测记录可查询", r["code"] == 0 and isinstance(r["data"]["items"], list), str(r))

    print("== 8. 告警配置与日志 ==")
    r = request("GET", "/api/alarm/config")
    check("读取阈值成功", r["code"] == 0 and "temp_max" in r["data"]["thresholds"], str(r))
    r = request("POST", "/api/alarm/config", {"temp_max": 50.0, "humidity_min": 10.0})
    check("保存阈值成功", r["code"] == 0 and r["data"]["thresholds"]["temp_max"] == 50.0, str(r))
    r = request("GET", "/api/alarms")
    check("告警日志可查询", r["code"] == 0 and isinstance(r["data"]["items"], list), str(r))
    r = request("GET", "/api/alarm/stats")
    check("告警统计可查询", r["code"] == 0 and isinstance(r["data"]["stats"], list), str(r))

    print("== 9. 操作日志与系统状态 ==")
    r = request("GET", f"/api/logs?lamp_id={lamp_id}")
    check("操作日志返回", r["code"] == 0 and isinstance(r["data"]["items"], list), str(r))
    r = request("GET", "/api/system")
    check("系统状态返回", r["code"] == 0 and "lamp_count" in r["data"] and "server_time" in r["data"], str(r))
    r = request("GET", "/api/sysconfig")
    check("系统配置可读取(cfg_system)", r["code"] == 0 and "lamp_posts" in r["data"], str(r))

    print("== 9.1 设备不在线告警 ==")
    # 用灯杆02（光照可正常读取的设备）测试：改地址→离线告警→恢复→告警解除
    sfc = request("GET", "/api/sysconfig")
    posts = sfc["data"]["lamp_posts"]
    target = next(p for p in posts if p["id"] == "02")
    orig_url = target.get("sensor_url", "")
    try:
        # 把传感器地址改成不可达 → 全部指标离线 → 产生 device_offline 告警
        target["sensor_url"] = "http://127.0.0.1:1/api/data"
        r = request("POST", "/api/sysconfig", {"lamp_posts": posts})
        check("保存不可达传感器地址", r["code"] == 0, str(r))
        time.sleep(7)
        r = request("GET", "/api/alarms?lamp_id=02&type=device_offline&status=active")
        items = r["data"]["items"]
        check("设备不在线告警已产生", r["code"] == 0 and len(items) > 0, str(r))
        # 恢复地址 → 指标重新在线 → 告警自动恢复
        target["sensor_url"] = orig_url
        r = request("POST", "/api/sysconfig", {"lamp_posts": posts})
        check("恢复传感器地址", r["code"] == 0, str(r))
        time.sleep(9)
        r = request("GET", "/api/alarms?lamp_id=02&type=device_offline&status=active")
        check("设备恢复后告警解除", len(r["data"]["items"]) == 0, str(r))
    finally:
        target["sensor_url"] = orig_url
        request("POST", "/api/sysconfig", {"lamp_posts": posts})

    print("== 10. 账号清理 ==")
    tr = request_raw("POST", "/api/auth/login", {"username": test_user, "password": "t123456"})
    me = request("GET", f"/api/auth/users", token=TOKEN)
    tid = next((u["id"] for u in me["data"]["users"] if u["username"] == test_user), None)
    r = request("DELETE", f"/api/auth/users/{tid}")
    check("删除测试用户", r["code"] == 0, str(r))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()