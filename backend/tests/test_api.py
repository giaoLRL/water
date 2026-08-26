"""后端接口功能测试：覆盖灯杆实时/详情/历史/统计/控制/告警/人员监测/系统状态/日志。

用法: python backend/tests/test_api.py [base_url]
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

passed = 0
failed = 0


def request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    check("监测记录可查询", r["code"] == 0 and isinstance(r["data"]["detections"], list), str(r))

    print("== 8. 告警配置与日志 ==")
    r = request("GET", "/api/alarm/config")
    check("读取阈值成功", r["code"] == 0 and "temp_max" in r["data"]["thresholds"], str(r))
    r = request("POST", "/api/alarm/config", {"temp_max": 50.0, "humidity_min": 10.0})
    check("保存阈值成功", r["code"] == 0 and r["data"]["thresholds"]["temp_max"] == 50.0, str(r))
    r = request("GET", "/api/alarms")
    check("告警日志可查询", r["code"] == 0 and isinstance(r["data"]["alarms"], list), str(r))

    print("== 9. 操作日志与系统状态 ==")
    r = request("GET", f"/api/logs?lamp_id={lamp_id}")
    check("操作日志返回", r["code"] == 0 and isinstance(r["data"]["logs"], list), str(r))
    r = request("GET", "/api/system")
    check("系统状态返回", r["code"] == 0 and "lamp_count" in r["data"] and "server_time" in r["data"], str(r))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()