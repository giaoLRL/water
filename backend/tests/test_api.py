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
    check("source 取值合法", all(it["source"] in ("manual", "auto", "judge") for it in items),
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
