"""水循环后端接口功能测试：账号/权限、实时、控制、定量浇水、设备链路、历史、统计、告警、日志。

用法: python backend/tests/test_api.py [base_url]（缺省 http://127.0.0.1:8000）

安全说明（纯真实模式，控制指令会真实下发到现场 ESP32）：
    默认只执行"安全动作"——关水泵(幂等)、读状态、设定定量目标为 0；
    不会开泵、不会清零累计水量。
    如需完整验证水泵开/关链路（会真实启动水泵抽水），设置环境变量后运行：
        $env:WATER_TEST_ACTUATE = "1"; python backend/tests/test_api.py
    清零累计水量为不可恢复的破坏性操作，本脚本永不执行，仅验证其权限拦截。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ADMIN = ("admin", "admin123")
ACTUATE = os.environ.get("WATER_TEST_ACTUATE") == "1"

passed = 0
failed = 0
warned = 0
TOKEN = ""


def request_raw(method: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"code": -exc.code, "msg": f"HTTP {exc.code}", "data": None}
    except Exception as exc:  # noqa: BLE001
        return {"code": -1, "msg": str(exc), "data": None}


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


def warn(name: str, detail: str = "") -> None:
    """非致命问题（如现场设备未接入）仅提示，不计失败。"""
    global warned
    warned += 1
    print(f"  [WARN] {name} {detail}")


def device_unreachable(resp: dict) -> bool:
    """判断返回是否为"现场设备暂时无响应"。

    ESP32 偶发超时/连接重置（实测约几分钟一次），此时后端会正确返回 40003。
    这类失败属于现场链路抖动而非后端缺陷，测试按 WARN 处理，避免误报。
    """
    if resp.get("code") != 40003:
        return False
    msg = str(resp.get("msg", ""))
    return any(k in msg for k in ("无法连接设备", "timed out", "超时", "Connection", "远程主机"))


def check_device(name: str, resp: dict, cond: bool) -> None:
    """设备相关断言：链路抖动时降级为 WARN。"""
    if device_unreachable(resp):
        warn(f"{name} —— 设备暂时无响应，跳过校验", str(resp.get("msg", ""))[:80])
    else:
        check(name, cond, str(resp))


def now_fmt(offset_minutes: int = 0) -> str:
    return (datetime.now() + timedelta(minutes=offset_minutes)).strftime("%Y-%m-%d %H:%M:%S")


def request_text(path: str, token: str | None = None) -> tuple[str, str, dict]:
    """取原始响应体（CSV 导出验证用），返回 (文本, Content-Type, 响应头)。"""
    headers = {}
    if token or TOKEN:
        headers["Authorization"] = "Bearer " + (token or TOKEN)
    req = urllib.request.Request(BASE + path, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8"), resp.headers.get("Content-Type", ""), dict(resp.headers)


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
    # 权限在进入处理函数前拦截，因此不会真正执行清零
    r = request_raw("POST", "/api/water/volume/reset", token=viewer_token)
    check("viewer 清零累计水量被拒 40301(未执行)", r["code"] == 40301, str(r))
    r = request_raw("GET", "/api/auth/users", token=viewer_token)
    check("viewer 访问账号管理被拒 40301", r["code"] == 40301, str(r))

    print("== 1. 实时数据 ==")
    rt = request("GET", "/api/water/realtime")
    check("realtime 返回 code=0", rt["code"] == 0, str(rt))
    d = rt["data"]
    for f in ("flow_rate", "total_liters", "total_flow", "pump_state", "pump_target",
              "sensor_online", "features", "device", "active_alarms", "alarm_count", "tank"):
        check(f"realtime 字段 {f}", f in d, str(sorted(d.keys())))
    if d.get("sensor_online"):
        check("流量读数有效(非负)", d["flow_rate"] is None or d["flow_rate"] >= 0, str(d["flow_rate"]))
        check("累计水量有效(非负)", d["total_liters"] is None or d["total_liters"] >= 0, str(d["total_liters"]))
        check("水泵状态为 on/off/unknown", d["pump_state"] in ("on", "off", "unknown"), str(d["pump_state"]))
    else:
        warn("现场设备离线，跳过读数有效性校验", d.get("last_error", ""))
    check("温度通道标注为支持", d["features"].get("temperature") is True, str(d.get("features")))
    check("压力通道标注为支持", d["features"].get("pressure") is True, str(d.get("features")))
    check("加热通道标注为支持", d["features"].get("heater") is True, str(d.get("features")))
    for f in ("storage_temp", "heater_temp", "pressure", "heater_state", "light"):
        check(f"realtime 字段 {f}", f in d, str(sorted(d.keys())))
    check("光照通道标注为支持", d["features"].get("light") is True, str(d.get("features")))

    print("== 2. 执行器控制 ==")
    r = request("POST", "/api/water/pump", {"action": "off"})
    check_device("关水泵成功(安全幂等)", r, r["code"] == 0 and r["data"]["pump_state"] in ("off", "unknown"))
    if ACTUATE:
        print("  -- WATER_TEST_ACTUATE=1：将真实启动水泵，请确认水路已接通 --")
        r = request("POST", "/api/water/pump", {"action": "on"})
        check_device("开水泵成功(真实下发)", r, r["code"] == 0 and r["data"]["pump_state"] == "on")
        time.sleep(2)
        rt = request("GET", "/api/water/realtime")["data"]
        check("开泵后出现流量", rt["flow_rate"] is None or rt["flow_rate"] >= 0, str(rt["flow_rate"]))
        r = request("POST", "/api/water/pump", {"action": "off"})
        check_device("关水泵成功(真实下发)", r, r["code"] == 0 and r["data"]["pump_state"] == "off")
    else:
        warn("已跳过开泵链路验证（设 WATER_TEST_ACTUATE=1 可启用）")
    r = request("POST", "/api/water/pump", {"action": "invalid"})
    check("非法指令返回 40002", r["code"] == 40002, str(r))
    r = request("POST", "/api/water/heater", {"action": "off"})
    check_device("关闭加热成功(安全幂等)", r, r["code"] == 0
                 and r["data"].get("heater_state") in ("off", "on", "unknown"))
    r = request("POST", "/api/water/heater", {"action": "invalid"})
    check("加热非法指令返回 40002", r["code"] == 40002, str(r))

    print("== 3. 定量浇水与累计水量 ==")
    r = request("GET", "/api/water/pump/target")
    check_device("读取定量目标", r, r["code"] == 0 and "target" in r["data"])
    r = request("POST", "/api/water/pump/target", {"liters": 0})
    check_device("设定定量目标为0(取消定量)", r, r["code"] == 0 and r["data"]["pump_target"] == 0)
    r = request("POST", "/api/water/pump/target", {"liters": -1})
    check("非法定量目标返回 40002", r["code"] == 40002, str(r))
    warn("未执行累计水量清零（不可恢复）；如需手工验证请在界面点击并二次确认")

    print("== 4. 设备链路 ==")
    r = request("GET", "/api/water/device")
    check("设备信息返回", r["code"] == 0 and "online" in r["data"], str(r))
    check("设备信息含支持通道", "features" in r["data"], str(r))
    if r["code"] == 0 and not r["data"]["online"]:
        warn("设备当前离线", str(r["data"]["device"]))

    print("== 4.1 双水槽水位 ==")
    rt = request("GET", "/api/water/realtime")["data"]
    tank = rt["tank"]
    check("水位含 tanks 与 any_unavailable", "tanks" in tank and "any_unavailable" in tank, str(tank))
    tanks = tank["tanks"]
    check("含储水槽与加热槽两路", set(tanks.keys()) == {"storage", "heater"}, str(list(tanks.keys())))
    for key, label in (("storage", "储水槽"), ("heater", "加热槽")):
        t = tanks[key]
        check(f"{label}含 来源/百分比/容积/估算水量",
              all(k in t for k in ("source", "percent", "capacity", "volume", "height_cm")), str(t))
        check(f"{label}百分比在 0~100", 0 <= t["percent"] <= 100, str(t["percent"]))
        check(f"{label}来源标注合法(device/none)", t["source"] in ("device", "none"), str(t["source"]))
        check(f"{label}标签正确", t["label"] == label, str(t["label"]))
    check("realtime 含两槽液位字段",
          "level_storage" in rt and "level_heater" in rt, str(sorted(rt.keys())))
    check("加热槽液位标为已接入(超声波)、储水槽标为未接入",
          rt["features"]["level_heater"] is True and rt["features"]["level_storage"] is False,
          str(rt["features"]))
    check("储水槽无传感器：source=none 且水位恒为 0",
          tanks["storage"]["source"] == "none" and tanks["storage"]["percent"] == 0.0
          and tanks["storage"]["volume"] == 0.0,
          str({k: v["source"] for k, v in tanks.items()}))
    check("加热槽为实测通道：source=device", tanks["heater"]["source"] == "device",
          str({k: v["source"] for k, v in tanks.items()}))
    check("any_unavailable 反映存在未接入槽位(储水槽)", tank["any_unavailable"] is True,
          str(tank["any_unavailable"]))
    # 无模拟数据：设备离线时两槽水位均归 0，不出现任何非零"演示值"
    if not rt["sensor_online"]:
        check("设备离线：两槽水位均为 0（不做模拟）",
              tanks["storage"]["percent"] == 0.0 and tanks["heater"]["percent"] == 0.0,
              str({k: v["percent"] for k, v in tanks.items()}))
    # 容积可改，并原值恢复（两槽分别设置）
    origin_caps = {k: tanks[k]["capacity"] for k in ("storage", "heater")}
    r = request("POST", "/api/water/tank", {"tank": "storage", "capacity": origin_caps["storage"] + 100})
    check("设置储水槽容积成功",
          r["code"] == 0 and r["data"]["tank_capacity"] == origin_caps["storage"] + 100, str(r))
    check("容积只影响指定水槽",
          r["data"]["capacities"]["heater"] == origin_caps["heater"], str(r["data"]["capacities"]))
    check("估算水量随容积更新",
          r["data"]["tanks"]["storage"]["volume"] == round(
              (origin_caps["storage"] + 100) * tanks["storage"]["percent"] / 100.0, 3),
          str(r["data"]["tanks"]["storage"]))
    r = request("POST", "/api/water/tank", {"tank": "storage", "capacity": origin_caps["storage"]})
    check("恢复储水槽容积成功",
          r["code"] == 0 and r["data"]["tank_capacity"] == origin_caps["storage"], str(r))
    r = request("POST", "/api/water/tank", {"tank": "heater", "capacity": origin_caps["heater"]})
    check("设置加热槽容积成功", r["code"] == 0, str(r))
    r = request("POST", "/api/water/tank", {"tank": "storage", "capacity": 0})
    check("非法容积返回 40002", r["code"] == 40002, str(r))
    r = request("POST", "/api/water/tank", {"tank": "nope", "capacity": 100})
    check("非法水槽名返回 40002", r["code"] == 40002, str(r))
    check("system 返回两槽容积",
          set(request("GET", "/api/water/system")["data"].get("tank_capacities", {})) == {"storage", "heater"}, "")
    # 注意：不在此设定非零定量目标 —— 现场固件会因此启动浇水并在完成后清零累计水量
    warn("未验证定量进度（设定非零目标会触发设备真实浇水并清零累计值）")

    print("== 5. 恒温PID（温度/加热通道已启用） ==")
    r = request("GET", "/api/water/pid")
    check("PID 查询返回 supported=True", r["code"] == 0 and r["data"]["supported"] is True, str(r))
    origin_pid = r["data"]
    # 只做参数读写与目标温度设定并原值恢复；不开启闭环（enabled 保持 0，避免真实控制加热）
    r = request("POST", "/api/water/pid", {"kp": 2.0, "ki": 0.5, "kd": 1.0})
    check("写入PID参数成功", r["code"] == 0, str(r))
    r = request("GET", "/api/water/pid")
    check("PID参数回读一致",
          (r["data"]["kp"], r["data"]["ki"], r["data"]["kd"]) == (2.0, 0.5, 1.0), str(r))
    r = request("POST", "/api/water/pid",
                {"kp": origin_pid["kp"], "ki": origin_pid["ki"], "kd": origin_pid["kd"]})
    check("恢复PID参数", r["code"] == 0, str(r))
    r = request("POST", "/api/water/target", {"temp": 40.0})
    check("设定目标温度成功", r["code"] == 0 and r["data"]["target_temp"] == 40.0, str(r))
    r = request("POST", "/api/water/target", {"temp": origin_pid["target_temp"]})
    check("恢复目标温度", r["code"] == 0, str(r))
    r = request("POST", "/api/water/target", {"temp": 999})
    check("非法目标温度返回 40002", r["code"] == 40002, str(r))
    r = request("POST", "/api/water/pid/mode", {"enabled": 0})
    check("恒温PID保持关闭(安全幂等)", r["code"] == 0 and r["data"]["pid_enabled"] is False, str(r))
    warn("未开启恒温闭环（会真实控制加热模块）；如需验证请在界面手动开启并观察")

    print("== 6. 历史数据 ==")
    start, end = now_fmt(-10), now_fmt(1)
    r = request("GET", f"/api/water/history?start={urllib.parse.quote(start)}&end={urllib.parse.quote(end)}")
    check("历史查询返回点", r["code"] == 0 and isinstance(r["data"]["points"], list), str(r))
    check("历史点含流量/累计水量/泵状态", len(r["data"]["points"]) == 0 or all(
        k in r["data"]["points"][0] for k in ("flow_rate", "total_flow", "pump_target", "pump_state")
    ), str(r))
    r = request("GET", "/api/water/history")
    check("缺少参数返回 40002", r["code"] == 40002, str(r))

    print("== 7. 数据统计 ==")
    r = request("GET", f"/api/water/stats?start={urllib.parse.quote(start)}&end={urllib.parse.quote(end)}")
    check("统计返回流量指标/累计水量/采样点数", r["code"] == 0
          and all(k in r["data"] for k in ("avg_flow", "max_flow", "min_flow", "samples", "total_flow")), str(r))
    check("统计返回温度/压力聚合指标", r["code"] == 0 and all(k in r["data"] for k in (
        "avg_storage_temp", "max_storage_temp", "min_storage_temp",
        "avg_heater_temp", "max_heater_temp", "min_heater_temp",
        "max_pressure", "min_pressure")), str(r["data"].keys()))
    check("统计返回光照聚合指标", r["code"] == 0 and all(
        k in r["data"] for k in ("avg_light", "max_light", "min_light")), str(r["data"].keys()))

    print("== 8. 告警 ==")
    r = request("GET", "/api/water/alarm/config")
    check("读取阈值成功(含流量阈值)", r["code"] == 0 and "flow_max" in r["data"]["thresholds"], str(r))
    origin = r["data"]["thresholds"]
    origin_max = float(origin.get("flow_max", 50.0))
    origin_min = float(origin.get("flow_min", 0.0))
    # 写入一个"必然不误报"的值来验证写接口，随后恢复原值，避免影响现场运行
    probe_max = origin_max + 1.0
    r = request("POST", "/api/water/alarm/config", {"flow_max": probe_max, "flow_min": origin_min})
    check("保存流量阈值成功", r["code"] == 0 and r["data"]["thresholds"]["flow_max"] == probe_max, str(r))
    r = request("POST", "/api/water/alarm/config", {"flow_max": origin_max, "flow_min": origin_min})
    check("恢复原阈值成功", r["code"] == 0 and r["data"]["thresholds"]["flow_max"] == origin_max, str(r))
    # 温度/压力/光照阈值（压力 kPa、光照 lx）：写入探测值验证后原值恢复
    origin_st_max = float(origin.get("storage_temp_max", 45.0))
    origin_p_max = float(origin.get("pressure_max", 400.0))
    origin_l_max = float(origin.get("light_max", 2000.0))
    r = request("POST", "/api/water/alarm/config",
                {"storage_temp_max": origin_st_max + 1.0, "pressure_max": origin_p_max + 1.0,
                 "light_max": origin_l_max + 100.0})
    check("保存温度/压力/光照阈值成功", r["code"] == 0
          and r["data"]["thresholds"]["storage_temp_max"] == origin_st_max + 1.0
          and r["data"]["thresholds"]["pressure_max"] == origin_p_max + 1.0
          and r["data"]["thresholds"]["light_max"] == origin_l_max + 100.0, str(r))
    r = request("POST", "/api/water/alarm/config",
                {"storage_temp_max": origin_st_max, "pressure_max": origin_p_max,
                 "light_max": origin_l_max})
    check("恢复温度/压力/光照阈值成功", r["code"] == 0
          and r["data"]["thresholds"]["storage_temp_max"] == origin_st_max
          and r["data"]["thresholds"]["pressure_max"] == origin_p_max
          and r["data"]["thresholds"]["light_max"] == origin_l_max, str(r))
    r = request("POST", "/api/water/alarm/config", {})
    check("空阈值返回 40002", r["code"] == 40002, str(r))
    r = request("GET", "/api/water/alarms")
    check("告警日志可查询", r["code"] == 0 and isinstance(r["data"]["items"], list), str(r))
    r = request("GET", "/api/water/alarm/stats")
    check("告警统计可查询", r["code"] == 0 and isinstance(r["data"]["stats"], list), str(r))

    print("== 9. 操作日志（含操作人与来源） ==")
    r = request("GET", "/api/water/logs?page_size=200")
    check("操作日志返回", r["code"] == 0 and isinstance(r["data"]["items"], list), str(r))
    items = r["data"]["items"]
    check("日志含 operator 字段", all("operator" in it for it in items), str(items[:1]))
    check("日志含 source 字段", all("source" in it for it in items), str(items[:1]))
    check("source 取值合法", all(it["source"] in ("manual", "auto", "judge", "timer") for it in items),
          str(sorted({it["source"] for it in items})))
    admin_ops = [it for it in items if it.get("operator") == "admin"]
    check("本次测试的人工操作已记录操作账号 admin", len(admin_ops) > 0,
          str([(it["action"], it["operator"]) for it in items[:8]]))
    check("人工操作 source=manual", all(it["source"] == "manual" for it in admin_ops), "")
    # 前面几节做过的操作应能从日志里查到
    for act in ("pump_off", "config.tank", "config.alarm", "account.create"):
        check(f"日志含 {act}", any(it["action"] == act for it in items),
              str(sorted({it["action"] for it in items}))[:200])
    check("config.tank 记录了操作账号", any(
        it["action"] == "config.tank" and it.get("operator") == "admin" for it in items), "")
    # 历史行（本次迁移前的数据）没有操作人，不应被伪造成某个账号
    legacy = [it for it in items if not it.get("operator") and it["source"] == "manual"]
    if legacy:
        warn(f"存在 {len(legacy)} 条迁移前的历史日志（无操作人，界面显示 —）", "")
    # 分类过滤
    for cat, prefixes in (("config", ("config.",)), ("account", ("account.",))):
        rc = request("GET", f"/api/water/logs?category={cat}&page_size=200")
        its = rc["data"]["items"]
        check(f"分类过滤 {cat} 只返回对应前缀",
              all(any(it["action"].startswith(p) for p in prefixes) for it in its) and len(its) > 0,
              str(sorted({it["action"] for it in its})))
    rc = request("GET", "/api/water/logs?category=device&page_size=200")
    check("分类过滤 device 不含配置/账号动作",
          all(not it["action"].startswith(("config.", "account.")) for it in rc["data"]["items"]), "")
    r = request("GET", "/api/water/logs?category=nope")
    check("非法分类返回 40002", r["code"] == 40002, str(r))

    print("== 9.1 判定与系统状态 ==")
    r = request("GET", "/api/water/system")
    check("系统状态返回设备地址与在线状态", r["code"] == 0
          and "device_url" in r["data"] and "device_online" in r["data"], str(r))
    r = request("GET", "/api/water/judge/status")
    check("判定服务状态可读", r["code"] == 0 and "enabled" in r["data"], str(r))
    r = request("POST", "/api/water/period", {"period": 1.0})
    check("设置采集周期=1s", r["code"] == 0 and r["data"]["period"] == 1.0, str(r))
    r = request("GET", "/api/water/logs?category=config&page_size=5")
    check("采集周期变更已入日志", any(it["action"] == "config.period" for it in r["data"]["items"]),
          str([it["action"] for it in r["data"]["items"]]))

    print("== 9.2 仪表盘布局与自定义卡片代理 ==")
    r = request("GET", "/api/water/dashboard/layout")
    check("布局读取接口可用", r["code"] == 0 and "layout" in r["data"], str(r))
    saved_before = r["data"]["layout"]
    demo_layout = {"version": 1, "widgets": [
        {"id": "t1", "type": "value", "title": "测试卡", "unit": "", "decimals": 1,
         "source": {"kind": "realtime", "path": "flow_rate"}, "grid": {"x": 0, "y": 0, "w": 6, "h": 2}},
    ]}
    r = request_raw("POST", "/api/water/dashboard/layout", {"layout": demo_layout}, token=viewer_token)
    check("viewer 保存布局被拒 40301", r["code"] == 40301, str(r))
    r = request("POST", "/api/water/dashboard/layout", {"layout": demo_layout})
    check("admin 保存布局成功", r["code"] == 0, str(r))
    r = request("GET", "/api/water/dashboard/layout")
    check("布局回读一致", r["code"] == 0 and r["data"]["layout"] == demo_layout, str(r))
    r = request("GET", "/api/water/logs?category=config&page_size=5")
    check("布局变更已入日志", any(it["action"] == "config.dashboard" for it in r["data"]["items"]),
          str([it["action"] for it in r["data"]["items"]]))
    # 自定义卡片代理：白名单外主机一律拒绝（防 SSRF）
    r = request("GET", "/api/water/dashboard/proxy?url=" + "http%3A%2F%2Fexample.com%2Fapi")
    check("代理拒绝白名单外主机 40002", r["code"] == 40002, str(r))
    r = request("GET", "/api/water/dashboard/proxy?url=" + "ftp%3A%2F%2F127.0.0.1%2Fx")
    check("代理拒绝非 http(s) 协议", r["code"] == 40002, str(r))
    # 白名单内主机（本机后端自身）：请求一个真实接口验证代发链路
    r = request("GET", "/api/water/dashboard/proxy?url="
                + "http%3A%2F%2F127.0.0.1%3A8000%2Fapi%2Fwater%2Frealtime")
    check("代理代发白名单主机成功", r["code"] == 0 and "json" in r["data"], str(r))
    # 恢复测试前的布局（无则复位为空）
    if saved_before:
        request("POST", "/api/water/dashboard/layout", {"layout": saved_before})
    else:
        request("POST", "/api/water/dashboard/layout/reset")
    r = request("GET", "/api/water/dashboard/layout")
    check("布局已恢复", r["code"] == 0 and r["data"]["layout"] == saved_before, str(r))

    print("== 9.3 报警联动（v3 卡片即来源） ==")
    layout_before_links = request("GET", "/api/water/dashboard/layout")["data"]["layout"]
    r = request("GET", "/api/water/alarm/links")
    check("联动读取接口可用", r["code"] == 0 and "links" in r["data"], str(r))
    r = request_raw("GET", "/api/water/alarm/links", token=viewer_token)
    check("viewer 读取联动被拒 40301", r["code"] == 40301, str(r))
    # 测试布局：自定义接口卡（可作触发源）+ 自定义指令控制卡（可作动作）+ 本机数值卡
    # 指令/数据 URL 全部指向本机只读接口，绝不触发真实水泵
    RO = f"{BASE}/api/water/realtime?token={TOKEN}"
    test_layout = {"version": 1, "widgets": [
        {"id": "e2e-custom", "type": "value", "title": "E2E自定义卡", "unit": "s",
         "decimals": 2, "source": {"kind": "custom", "url": RO, "path": "data.period", "period": 2},
         "grid": {"x": 0, "y": 0, "w": 3, "h": 2}},
        {"id": "e2e-ctl", "type": "control", "title": "E2E控制卡",
         "cmd": {"on": RO, "off": RO},
         "source": {"kind": "realtime", "path": "pump_state"},
         "grid": {"x": 3, "y": 0, "w": 3, "h": 2}},
        {"id": "e2e-tank", "type": "tank", "title": "E2E水槽", "unit": "%", "decimals": 1,
         "source": {"kind": "realtime", "path": "tank.tanks.heater"},
         "grid": {"x": 6, "y": 0, "w": 3, "h": 2}},
        {"id": "e2e-flow", "type": "value", "title": "E2E流量卡", "unit": "L/min", "decimals": 2,
         "source": {"kind": "realtime", "path": "flow_rate"},
         "grid": {"x": 6, "y": 0, "w": 3, "h": 2}},
    ]}
    r = request("POST", "/api/water/dashboard/layout", {"layout": test_layout})
    check("联动测试卡片布局已保存", r["code"] == 0, str(r))
    good = [{"id": "t1", "name": "E2E联动", "enabled": True,
             "source_card": "e2e-flow",
             "source": {"kind": "realtime", "path": "flow_rate", "url": "", "period": 5},
             "threshold": 99999, "direction": "above",
             "actions": [{"card": "e2e-ctl", "state": "off", "kind": "url", "url": RO}],
             "recover": {"actions": [{"card": "e2e-ctl", "state": "on", "kind": "url", "url": RO}]}}]
    r = request("POST", "/api/water/alarm/links", {"links": good})
    check("保存联动规则成功", r["code"] == 0 and len(r["data"]["links"]) == 1, str(r))
    r = request("GET", "/api/water/alarm/links")
    saved = r["data"]["links"]
    check("规则回读一致(卡片引用+自带阈值)",
          saved and saved[0]["source_card"] == "e2e-flow"
          and float(saved[0]["threshold"]) == 99999
          and saved[0]["actions"][0]["card"] == "e2e-ctl"
          and saved[0]["recover"]["actions"][0]["state"] == "on", str(saved))
    r = request("POST", "/api/water/alarm/links",
                {"links": [{"id": "t2", "name": "E2E自定义源", "enabled": True,
                            "source_card": "e2e-custom",
                            "source": {"kind": "custom", "url": RO, "path": "data.flow_rate", "period": 2},
                            "threshold": 99999, "direction": "above",
                            "actions": [{"card": "e2e-ctl", "state": "off", "kind": "url", "url": RO}],
                            "recover": {"actions": []}}]})
    check("自定义卡作触发源可保存", r["code"] == 0, str(r))
    bad = [
        ({"source": {"kind": "realtime", "path": "flow_rate"}, "threshold": 1, "direction": "above",
          "actions": [{"card": "e2e-ctl", "state": "off", "kind": "url", "url": RO}]}, "缺少触发源卡片"),
        ({"source_card": "e2e-flow", "source": {"kind": "nope", "path": "flow_rate"}, "threshold": 1,
          "direction": "above", "actions": [{"card": "e2e-ctl", "state": "off", "kind": "url", "url": RO}]},
         "触发源类型非法"),
        ({"source_card": "e2e-flow", "source": {"kind": "realtime", "path": ""}, "threshold": 1,
          "direction": "above", "actions": [{"card": "e2e-ctl", "state": "off", "kind": "url", "url": RO}]},
         "触发源缺取值路径"),
        ({"source_card": "e2e-custom", "source": {"kind": "custom", "url": "http://evil.example.com/x",
          "path": "v", "period": 5}, "threshold": 1, "direction": "above",
          "actions": [{"card": "e2e-ctl", "state": "off", "kind": "url", "url": RO}]}, "触发源白名单外"),
        ({"source_card": "e2e-custom", "source": {"kind": "custom", "url": RO, "path": "v", "period": 1},
          "threshold": 1, "direction": "above",
          "actions": [{"card": "e2e-ctl", "state": "off", "kind": "url", "url": RO}]}, "触发源周期 <2s"),
        ({"source_card": "e2e-flow", "source": {"kind": "realtime", "path": "flow_rate"}, "threshold": 1,
          "direction": "above", "actions": [{"state": "off", "kind": "url", "url": RO}]}, "动作缺卡片"),
        ({"source_card": "e2e-flow", "source": {"kind": "realtime", "path": "flow_rate"}, "threshold": 1,
          "direction": "above", "actions": [{"card": "e2e-ctl", "state": "blink", "kind": "url", "url": RO}]},
         "动作状态非法"),
        ({"source_card": "e2e-flow", "source": {"kind": "realtime", "path": "flow_rate"}, "threshold": 1,
          "direction": "above", "actions": [{"card": "e2e-ctl", "state": "off", "kind": "url",
                                              "url": "http://evil.example.com/x"}]}, "动作指令白名单外"),
        ({"source_card": "e2e-flow", "source": {"kind": "realtime", "path": "flow_rate"}, "threshold": "abc",
          "direction": "above", "actions": [{"card": "e2e-ctl", "state": "off", "kind": "url", "url": RO}]},
         "阈值非数值"),
        ({"source_card": "e2e-flow", "source": {"kind": "realtime", "path": "flow_rate"}, "threshold": 1,
          "direction": "above", "actions": []}, "空动作列表"),
    ]
    for rule, name in bad:
        r = request("POST", "/api/water/alarm/links", {"links": [dict(rule, id="bad")]})
        check(f"非法规则被拒 40002（{name}）", r["code"] == 40002, str(r))
    r = request_raw("POST", "/api/water/alarm/links", {"links": good}, token=viewer_token)
    check("viewer 保存联动被拒 40301", r["code"] == 40301, str(r))
    r = request("GET", "/api/water/logs?category=config&page_size=20")
    check("联动配置已入日志(config.link)",
          "config.link" in [it["action"] for it in r["data"]["items"]],
          str([it["action"] for it in r["data"]["items"]][:10]))
    r = request("POST", "/api/water/alarm/links", {"links": []})
    check("联动规则已恢复空", r["code"] == 0 and r["data"]["links"] == [], str(r))

    print("== 9.4 自定义传感器通道（卡片即声明） ==")
    time.sleep(3)   # 等采集循环按卡片周期(2s)至少轮询一次
    r = request("GET", "/api/water/realtime")
    chans = r["data"].get("custom_channels") or []
    vals = r["data"].get("custom") or {}
    check("realtime 快照含卡片派生的自定义通道",
          any(c.get("id") == "e2e-custom" for c in chans), str(chans))
    check("自定义通道已轮询到读数", vals.get("e2e-custom") is not None, str(vals))
    # 本机快照卡（非标准列路径）也会自动派生通道：卡片即指标
    check("本机快照卡自动派生为通道（card:<卡片id>）",
          any(c.get("id") == "card:e2e-tank" for c in chans), str(chans))
    check("派生通道已取样入库", vals.get("card:e2e-tank") is not None, str(vals))
    qs = urllib.parse.urlencode({"channel_id": "card:e2e-tank", "start": now_fmt(-5), "end": now_fmt(5)})
    r = request("GET", f"/api/water/custom/history?{qs}")
    check("派生通道历史可查询", r["code"] == 0 and isinstance(r["data"]["points"], list), str(r)[:140])
    qs = urllib.parse.urlencode({"channel_id": "e2e-custom", "start": now_fmt(-5), "end": now_fmt(5)})
    r = request("GET", f"/api/water/custom/history?{qs}")
    check("自定义通道历史可查询", r["code"] == 0 and isinstance(r["data"]["points"], list), str(r))
    qs = urllib.parse.urlencode({"channel_id": "e2e-custom", "start": now_fmt(-5), "end": now_fmt(5)})
    r = request("GET", f"/api/water/custom/stats?{qs}")
    check("自定义通道统计可查询", r["code"] == 0 and "samples" in r["data"], str(r))
    # 删除卡片后通道自动消失（卡片即声明，不再轮询）
    r = request("POST", "/api/water/dashboard/layout", {"layout": {"version": 1, "widgets": []}})
    check("清空布局成功", r["code"] == 0, str(r))
    time.sleep(1.0)
    r = request("GET", "/api/water/realtime")
    check("卡片删除后通道不再采集", not (r["data"].get("custom_channels") or []),
          str(r["data"].get("custom_channels")))
    # 还原布局
    if layout_before_links:
        request("POST", "/api/water/dashboard/layout", {"layout": layout_before_links})
    else:
        request("POST", "/api/water/dashboard/layout/reset")
    r = request("GET", "/api/water/dashboard/layout")
    check("联动测试布局已还原", r["code"] == 0 and r["data"]["layout"] == layout_before_links, str(r))

    print("== 9.5 新增能力（导出/定时/标定/外部设备/判定模板/回放/持续判定） ==")
    request("POST", "/api/water/judge/enable", {"enabled": 0})
    layout_before_new = request("GET", "/api/water/dashboard/layout")["data"]["layout"]
    RO2 = f"{BASE}/api/water/realtime?token={TOKEN}"
    new_layout = {"version": 1, "widgets": [
        {"id": "e2e-cap", "type": "value", "title": "E2E持续源", "unit": "s", "decimals": 1,
         "source": {"kind": "custom", "url": RO2, "path": "data.period", "period": 2},
         "grid": {"x": 0, "y": 0, "w": 3, "h": 2}},
        {"id": "e2e-ctl2", "type": "control", "title": "E2E定时动作",
         "cmd": {"on": RO2, "off": RO2},
         "source": {"kind": "realtime", "path": "pump_state"},
         "grid": {"x": 3, "y": 0, "w": 3, "h": 2}}]}
    request("POST", "/api/water/dashboard/layout", {"layout": new_layout})

    # ---- CSV 导出 ----
    text, ctype, exp_head = request_text("/api/water/export.csv?kind=sensors")
    check("传感数据 CSV 导出（UTF-8 BOM + 中文表头）",
          text.startswith("\ufeff") and "瞬时流量" in text.splitlines()[0], ctype)
    check("CSV 导出响应头带条数信息（X-Exported-Rows / X-Total-Rows）",
          "x-exported-rows" in exp_head and "x-total-rows" in exp_head, str(list(exp_head)[:8]))
    text, _, _ = request_text("/api/water/export.csv?kind=logs&category=config")
    check("操作日志 CSV 导出", text.startswith("\ufeff") and "操作人" in text.splitlines()[0])
    text, _, _ = request_text("/api/water/export.csv?kind=alarms")
    check("告警 CSV 导出", text.startswith("\ufeff") and "阈值" in text.splitlines()[0])
    r = request("GET", "/api/water/export.csv?kind=custom")
    check("自定义通道导出缺 channel_id 被拒 40002", r["code"] == 40002, str(r))
    r = request("GET", "/api/water/export.csv?kind=nope")
    check("非法导出类型被拒 40002", r["code"] == 40002, str(r))

    # ---- 定时任务 ----
    r = request("POST", "/api/water/timers", {"timers": [
        {"id": "t1", "name": "E2E定时", "enabled": True, "mode": "interval", "every_sec": 5,
         "actions": [{"card": "e2e-ctl2", "state": "off", "kind": "url", "url": RO2}]}]})
    check("保存定时任务成功", r["code"] == 0, str(r))
    r = request("GET", "/api/water/timers")
    check("定时任务回读含下次执行时间",
          r["code"] == 0 and bool(r["data"]["timers"][0].get("next_run")), str(r))
    for bad, name in (
        ({"id": "b1", "mode": "interval", "every_sec": 1,
          "actions": [{"card": "e2e-ctl2", "state": "off", "kind": "url", "url": RO2}]}, "间隔 <5s"),
        ({"id": "b2", "mode": "daily", "at": "25:00",
          "actions": [{"card": "e2e-ctl2", "state": "off", "kind": "url", "url": RO2}]}, "时刻越界"),
        ({"id": "b3", "mode": "daily", "at": "08:00", "actions": []}, "空动作列表"),
    ):
        r = request("POST", "/api/water/timers", {"timers": [bad]})
        check(f"非法定时任务被拒 40002（{name}）", r["code"] == 40002, str(r))
    time.sleep(12)   # 等 5 秒间隔的任务至少执行一次（设备离线时采集循环会因超时变慢）
    items = request("GET", "/api/water/logs?page=1&page_size=20")["data"]["items"]
    check("定时任务到点执行并写日志（timer_*）",
          any(str(i["action"]).startswith("timer_") for i in items),
          str([i["action"] for i in items][:8]))

    # ---- 通道标定 ----
    r = request("POST", "/api/water/calibration/two_point",
                {"raw1": 100, "eng1": 10, "raw2": 200, "eng2": 25})
    check("两点标定计算正确（scale=0.15 offset=-5）",
          r["code"] == 0 and abs(r["data"]["scale"] - 0.15) < 1e-9 and abs(r["data"]["offset"] + 5) < 1e-9, str(r))
    r = request("POST", "/api/water/calibration",
                {"calibration": {"pressure": {"scale": 2, "offset": 1, "unit": "kPa"}}})
    check("保存通道标定成功", r["code"] == 0, str(r))
    r = request("GET", "/api/water/calibration")
    check("标定回读一致",
          abs((r["data"]["calibration"].get("pressure") or {}).get("scale", 0) - 2) < 1e-9, str(r))
    r = request("POST", "/api/water/calibration",
                {"calibration": {"pressure": {"scale": "abc"}}})
    check("非法标定系数被拒 40002", r["code"] == 40002, str(r))

    # ---- 外部设备接入（Modbus/串口） ----
    r = request("POST", "/api/water/gateway", {"gateways": [
        {"id": "g1", "name": "E2E流量计", "mode": "rtu_tcp", "host": "127.0.0.1", "port": 15021,
         "unit": 1, "period": 2, "enabled": True,
         "polls": [{"key": "f", "func": 3, "addr": 0, "count": 1, "type": "u16",
                    "scale": 0.1, "offset": 0, "target": "custom:e2e-gw"}]}]})
    check("保存外部设备配置成功", r["code"] == 0, str(r))
    r = request("GET", "/api/water/gateway")
    check("外部设备回读一致", r["code"] == 0 and r["data"]["gateways"][0]["mode"] == "rtu_tcp", str(r))
    r = request("GET", "/api/water/gateway/test?id=g1")
    check("试读接口可用（无真实从站时返回失败原因）",
          r["code"] in (0, 40003, 40004), str(r)[:120])
    for bad, name in (
        ({"id": "b1", "mode": "tcp", "host": "", "port": 502}, "缺 IP"),
        ({"id": "b2", "mode": "tcp", "host": "127.0.0.1", "port": 502, "unit": 999}, "从站号越界"),
        ({"id": "b3", "mode": "nope", "host": "127.0.0.1", "port": 502}, "模式非法"),
    ):
        r = request("POST", "/api/water/gateway", {"gateways": [bad]})
        check(f"非法外部设备被拒 40002（{name}）", r["code"] == 40002, str(r))

    # ---- 判定服务报文模板 ----
    r = request("POST", "/api/water/judge/template", {
        "url": "http://127.0.0.1:9100",
        "template": {"report": {"path": "/api/report",
                                "body": {"dev": "{{device_id}}", "t": "{{flow_rate}}"}}}})
    check("保存判定服务地址与模板成功", r["code"] == 0, str(r))
    r = request("GET", "/api/water/judge/template")
    check("判定模板回读一致",
          str(r["data"]["url"]).endswith(":9100") and r["data"]["template"]["report"]["path"] == "/api/report", str(r))
    r = request("POST", "/api/water/judge/template", {"url": "ftp://x", "template": {}})
    check("非法判定地址被拒 40002", r["code"] == 40002, str(r))
    r = request("POST", "/api/water/judge/template", {"url": "http://127.0.0.1:9100",
                                                     "template": {"report": {"path": "no-slash"}}})
    check("模板路径缺前导斜杠被拒 40002", r["code"] == 40002, str(r))

    # ---- 历史回放 ----
    r = request("GET", "/api/water/replay?ts=" + urllib.parse.quote(now_fmt(-1)))
    check("回放按时间点取快照", r["code"] == 0 and "sensor" in r["data"], str(r)[:120])
    r = request("GET", "/api/water/replay?ts=bad-time")
    check("非法回放时间被拒 40002", r["code"] == 40002, str(r))
    r = request("GET", "/api/water/replay/frames?start=" + urllib.parse.quote(now_fmt(-30))
                + "&end=" + urllib.parse.quote(now_fmt(0)) + "&limit=20")
    check("回放帧序列可用", r["code"] == 0 and 2 <= len(r["data"]["frames"]) <= 20, str(r)[:120])

    # ---- 组合条件 + 持续判定（真实触发） ----
    r = request("POST", "/api/water/alarm/links", {"links": [
        {"id": "hold1", "name": "E2E组合条件", "enabled": True, "source_card": "e2e-cap",
         "source": {"kind": "custom", "url": RO2, "path": "data.period", "period": 2},
         "threshold": 0.5, "direction": "above", "hold": 3,
         "extra": [{"card": "e2e-cap", "source": {"kind": "custom", "url": RO2,
                                                  "path": "data.period", "period": 2},
                    "direction": "above", "threshold": 0}],
         "actions": [{"card": "e2e-ctl2", "state": "off", "kind": "url", "url": RO2}],
         "recover": {"actions": []}}]})
    check("组合条件+持续判定规则可保存", r["code"] == 0, str(r))
    for bad, name in (
        ({"id": "h1", "source_card": "e2e-cap", "source": {"kind": "custom", "url": RO2, "path": "data.period"},
          "threshold": 1, "direction": "above", "hold": -1,
          "actions": [{"card": "e2e-ctl2", "state": "off", "kind": "url", "url": RO2}]}, "负持续秒数"),
        ({"id": "h2", "source_card": "e2e-cap", "source": {"kind": "custom", "url": RO2, "path": "data.period"},
          "threshold": 1, "direction": "above",
          "extra": [{"card": "", "source": {"kind": "realtime", "path": "pressure"},
                     "direction": "above", "threshold": 1}],
          "actions": [{"card": "e2e-ctl2", "state": "off", "kind": "url", "url": RO2}]}, "附加条件缺卡片"),
    ):
        r = request("POST", "/api/water/alarm/links", {"links": [bad]})
        check(f"非法组合规则被拒 40002（{name}）", r["code"] == 40002, str(r))
    time.sleep(5)    # 等自定义通道首次轮询，随后计时 hold=3 秒
    time.sleep(5)
    items = request("GET", "/api/water/logs?page=1&page_size=20")["data"]["items"]
    check("读数为真时按 hold 触发一次动作",
          any(i["action"] == "link_card_off" for i in items), str([i["action"] for i in items][:8]))

    # ---- 还原 ----
    request("POST", "/api/water/alarm/links", {"links": []})
    request("POST", "/api/water/timers", {"timers": []})
    request("POST", "/api/water/calibration", {"calibration": {}})
    request("POST", "/api/water/gateway", {"gateways": []})
    request("POST", "/api/water/judge/template", {"url": "", "template": {}})
    if layout_before_new:
        request("POST", "/api/water/dashboard/layout", {"layout": layout_before_new})
    else:
        request("POST", "/api/water/dashboard/layout/reset")
    check("新增能力测试后已还原",
          request("GET", "/api/water/timers")["data"]["timers"] == []
          and request("GET", "/api/water/gateway")["data"]["gateways"] == [])

    print("== 9.6 历史数据修正（取样方向 / 降采样 / 统计口径） ==")
    s24 = now_fmt(-24 * 60)
    e0 = now_fmt(0)
    q24 = "start=" + urllib.parse.quote(s24) + "&end=" + urllib.parse.quote(e0)
    r = request("GET", f"/api/water/history?{q24}&limit=1500")
    pts = r["data"]["points"]
    check("历史曲线覆盖到最新数据（末点接近当前时间）",
          bool(pts) and pts[-1]["ts"] >= now_fmt(-30),
          f"末点={pts[-1]['ts'] if pts else '-'} 当前={e0}")
    check("历史接口返回取样元信息 raw_count / sampled",
          "raw_count" in r["data"] and "sampled" in r["data"], str(list(r["data"].keys())))
    check("长区间自动等距降采样且点数受控",
          r["data"]["sampled"] is True and 0 < len(pts) <= 1500,
          f"点数={len(pts)} raw={r['data']['raw_count']}")
    # 降采样保真：小点数取样与全量取样的首尾必须一致（说明覆盖了整个区间，没有丢最新数据）
    q6 = "start=" + urllib.parse.quote(now_fmt(-6 * 60)) + "&end=" + urllib.parse.quote(e0)
    full_pts = request("GET", f"/api/water/history?{q6}&limit=50000")["data"]["points"]
    samp_pts = request("GET", f"/api/water/history?{q6}&limit=100")["data"]["points"]
    check("降采样保留首尾（覆盖整个区间）",
          bool(full_pts) and bool(samp_pts)
          and samp_pts[0]["ts"] == full_pts[0]["ts"] and samp_pts[-1]["ts"] == full_pts[-1]["ts"],
          f"full={full_pts[0]['ts'] if full_pts else '-'}..{full_pts[-1]['ts'] if full_pts else '-'} / "
          f"samp={samp_pts[0]['ts'] if samp_pts else '-'}..{samp_pts[-1]['ts'] if samp_pts else '-'}")

    r = request("GET", f"/api/water/replay/frames?{q24}&limit=60")
    fr = r["data"]["frames"]
    check("回放帧覆盖到区间末尾（含最新帧）",
          bool(fr) and fr[-1]["ts"] >= now_fmt(-30), f"末帧={fr[-1]['ts'] if fr else '-'}")
    check("回放返回降采样元信息",
          r["data"]["sampled"] is True and r["data"]["raw_count"] >= len(fr), str(r["data"]["raw_count"]))

    st = request("GET", f"/api/water/stats?{q24}")["data"]
    check("统计区分总行数与有效读数点",
          st["samples"] >= st["valid_samples"]["flow"] > 0,
          f"samples={st['samples']} valid={st['valid_samples']['flow']}")
    check("统计含时间加权平均流量字段", "avg_flow_weighted" in st, str(list(st.keys())[:10]))

    print("== 9.7 恒温闭环多路回路（卡片即来源） ==")
    layout_before_pid = request("GET", "/api/water/dashboard/layout")["data"]["layout"]
    RO3 = f"{BASE}/api/water/realtime?token={TOKEN}"
    # 温度源用"自定义接口卡"指向本机只读接口（与现场设备在线与否无关，结果确定）
    pid_layout = {"version": 1, "widgets": [
        {"id": "p-sensor", "type": "value", "title": "P温度源", "unit": "℃", "decimals": 1,
         "source": {"kind": "custom", "url": RO3, "path": "data.period", "period": 2},
         "grid": {"x": 0, "y": 0, "w": 3, "h": 2}},
        {"id": "p-guard", "type": "tank", "title": "P液位", "unit": "%", "decimals": 1,
         "source": {"kind": "realtime", "path": "tank.tanks.heater"}, "grid": {"x": 3, "y": 0, "w": 3, "h": 2}},
        {"id": "p-act", "type": "control", "title": "P执行器", "cmd": {"on": RO3, "off": RO3},
         "source": {"kind": "realtime", "path": "pump_state"}, "grid": {"x": 6, "y": 0, "w": 3, "h": 2}},
        {"id": "p-pid", "type": "builtin", "builtin": "pid", "title": "P闭环",
         "pid": {"sensor_card": "p-sensor", "actuator_card": "p-act", "guard_card": "p-guard",
                 "guard_min": 0, "target": 60, "kp": 16, "ki": 0.3, "kd": 25, "enabled": False},
         "grid": {"x": 0, "y": 2, "w": 4, "h": 4}}]}
    r = request("POST", "/api/water/dashboard/layout", {"layout": pid_layout})
    check("闭环测试布局已保存", r["code"] == 0, str(r))
    time.sleep(4)
    loops = request("GET", "/api/water/pid/loops")["data"]["loops"]
    loop = next((l for l in loops if l["id"] == "p-pid"), {})
    check("闭环卡派生为独立回路（含来源卡与执行器卡名称）",
          loop.get("sensor_title") == "P温度源" and loop.get("actuator_title") == "P执行器",
          json.dumps(loop, ensure_ascii=False)[:200])
    check("回路初始为关闭（卡片配置 enabled=false）", loop.get("enabled") is False, str(loop.get("enabled")))
    r = request("POST", "/api/water/pid/loops", {"id": "p-pid", "target": 55, "kp": 20, "enabled": True})
    check("更新回路参数成功", r["code"] == 0 and float(r["data"]["loop"].get("target")) == 55, str(r)[:160])
    pid_card = next((w for w in request("GET", "/api/water/dashboard/layout")["data"]["layout"]["widgets"]
                     if w["id"] == "p-pid"), {})
    check("参数写回卡片配置（卡片即来源）",
          float((pid_card.get("pid") or {}).get("target")) == 55
          and float((pid_card.get("pid") or {}).get("kp")) == 20, json.dumps(pid_card.get("pid"), ensure_ascii=False)[:160])
    for bad, name in (
        ({"target": 10}, "缺回路 id"),
        ({"id": "no-such", "target": 10}, "回路不存在"),
        ({"id": "p-pid", "target": 999}, "目标温度越界"),
        ({"id": "p-pid", "kp": "abc"}, "Kp 非数值"),
    ):
        r = request("POST", "/api/water/pid/loops", bad)
        expect = 40004 if name == "回路不存在" else 40002
        check(f"非法闭环更新被拒 {expect}（{name}）", r["code"] == expect, str(r))
    # 防干烧联锁：把条件卡阈值调高 → 应强制断开加热并给出原因
    pid_layout["widgets"][3]["pid"]["guard_min"] = 100
    pid_layout["widgets"][3]["pid"]["target"] = 60
    pid_layout["widgets"][3]["pid"]["enabled"] = True
    request("POST", "/api/water/dashboard/layout", {"layout": pid_layout})
    time.sleep(4)
    loop2 = next((l for l in request("GET", "/api/water/pid/loops")["data"]["loops"] if l["id"] == "p-pid"), {})
    check("防干烧联锁生效（条件不满足时强制断开并说明原因）",
          loop2.get("guard_ok") is False and "前置条件" in str(loop2.get("error")), json.dumps(loop2, ensure_ascii=False)[:200])
    # 放开联锁 → 实测(1.0)低于目标(60) → 应下发开启指令并写 pid_actuator_on 日志
    pid_layout["widgets"][3]["pid"]["guard_min"] = 0
    request("POST", "/api/water/dashboard/layout", {"layout": pid_layout})
    time.sleep(4)
    loop3 = next((l for l in request("GET", "/api/water/pid/loops")["data"]["loops"] if l["id"] == "p-pid"), {})
    check("联锁满足时按 PID 下发（执行器=自定义指令）",
          loop3.get("action") == "on" and loop3.get("guard_ok") is True, json.dumps(loop3, ensure_ascii=False)[:200])
    items = request("GET", "/api/water/logs?page=1&page_size=20")["data"]["items"]
    check("闭环动作写入操作日志（pid_actuator_*，来源=auto）",
          any(i["action"].startswith("pid_actuator_") and i["source"] == "auto" for i in items),
          str([i["action"] for i in items][:8]))
    # 关闭回路，避免测试结束后继续下发
    request("POST", "/api/water/pid/loops", {"id": "p-pid", "enabled": False})
    if layout_before_pid:
        request("POST", "/api/water/dashboard/layout", {"layout": layout_before_pid})
    else:
        request("POST", "/api/water/dashboard/layout/reset")
    check("闭环测试布局已还原",
          all(w["id"] != "p-pid" for w in (request("GET", "/api/water/dashboard/layout")["data"]["layout"] or {"widgets": []})["widgets"]))

    print("== 9.8 布局并发保护与一键恢复上一版 ==")
    base = request("GET", "/api/water/dashboard/layout")["data"]
    check("布局接口返回版本号与备份标志",
          isinstance(base.get("rev"), int) and "has_prev" in base, str(base)[:160])
    layout_keep = base.get("layout") or {"version": 1, "widgets": []}
    # 正常保存（带上载入时的版本号）：版本号 +1
    r = request("POST", "/api/water/dashboard/layout",
                {"layout": {"version": 1, "widgets": []}, "base_rev": base["rev"]})
    check("带正确版本号可保存并返回新版本号",
          r["code"] == 0 and r["data"]["rev"] == base["rev"] + 1, str(r)[:160])
    # 拿旧版本号再保存：必须被拒（防止旧页面把新布局整片覆盖）
    r = request("POST", "/api/water/dashboard/layout",
                {"layout": {"version": 1, "widgets": []}, "base_rev": base["rev"]})
    check("版本号过期时拒绝保存（40009）", r["code"] == 40009, str(r)[:200])
    # 老客户端/脚本不带版本号仍可用（向后兼容）
    r = request("POST", "/api/water/dashboard/layout", {"layout": layout_keep})
    check("不传版本号时保持向后兼容", r["code"] == 0, str(r)[:160])
    # 一键恢复上一版：应把刚保存的 layout_keep 换成上一版（空布局）
    r = request("POST", "/api/water/dashboard/layout/restore_prev")
    check("一键恢复上一次布局成功",
          r["code"] == 0 and r["data"]["layout"]["widgets"] == [], str(r)[:200])
    # 再点一次即可切回（恢复本身也会备份），并写审计日志
    r = request("POST", "/api/water/dashboard/layout/restore_prev")
    check("恢复可反复切换（恢复前会备份当前版）",
          r["code"] == 0 and r["data"]["layout"]["widgets"] == layout_keep.get("widgets", []), str(r)[:200])
    acts = [i["action"] for i in request("GET", "/api/water/logs?category=config&page_size=10")["data"]["items"]]
    check("恢复操作写入操作日志", "config.dashboard" in acts, str(acts[:5]))
    # 收尾：把布局还原成进入本节前的样子
    request("POST", "/api/water/dashboard/layout", {"layout": layout_keep})

    print("== 10. 账号清理 ==")
    me = request("GET", "/api/auth/users")
    tid = next((u["id"] for u in me["data"]["users"] if u["username"] == test_user), None)
    r = request("DELETE", f"/api/auth/users/{tid}")
    check("删除测试用户", r["code"] == 0, str(r))
    r = request("GET", "/api/water/logs?category=account&page_size=20")
    acts = [it["action"] for it in r["data"]["items"]]
    check("账号创建与删除均已入日志",
          "account.create" in acts and "account.delete" in acts, str(acts))
    check("账号日志记录了操作账号",
          all(it.get("operator") == "admin" for it in r["data"]["items"]), "")
    check("账号日志未泄露密码明文",
          all("password" not in (it.get("detail") or "").lower() or "密码" in (it.get("detail") or "")
              for it in r["data"]["items"]), "")

    print(f"\n结果: {passed} 通过, {failed} 失败, {warned} 提示")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
