r"""双水槽模拟液位的确定性单元测试：守恒转移、回平、边界、长暂停保护、来源标记。

液位传感器尚未接入，此阶段两槽水位由 WaterPlant._simulate_levels() 生成；
本测试不访问网络与数据库，可独立运行：
    python backend/tests/test_level_sim.py
"""
import os
import sys
import time

# 让脚本无论从哪个目录运行都能 import 到 backend 下的模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                        # noqa: E402
from water_device import WaterPlant  # noqa: E402

bad = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global bad
    if cond:
        print(f"  [PASS] {name}")
    else:
        bad += 1
        print(f"  [FAIL] {name} {detail}")


plant = WaterPlant()
plant._last_level_ts = time.time()


def step(flow: float, storage: float, heater: float, seconds: float):
    """把设备置于指定流量与两槽水位，模拟经过 seconds 秒后执行一次液位模拟。"""
    plant.flow_rate = flow
    plant.level_storage = storage
    plant.level_heater = heater
    plant._last_level_ts = time.time() - seconds
    plant._simulate_levels()
    return plant.level_storage, plant.level_heater


print("== 水泵运行时：储水槽 → 加热槽 ==")
s0, h0 = 60.0, 30.0
s1, h1 = step(config.SIM_LEVEL_FLOW_REF, s0, h0, 1.0)
print(f"  1s 后 储水槽 {s1:.4f}%  加热槽 {h1:.4f}%")
check("储水槽水位下降", s1 < s0, f"{s0} -> {s1}")
check("加热槽水位上升", h1 > h0, f"{h0} -> {h1}")
check("转移速率≈配置值（满速）",
      abs((h1 - h0) - config.SIM_LEVEL_TRANSFER_PER_S) < 0.02,
      f"实际转移 {h1 - h0:.4f} 期望 {config.SIM_LEVEL_TRANSFER_PER_S}")
check("水量守恒（两槽总量不变）", abs((s1 + h1) - (s0 + h0)) < 1e-6,
      f"前 {s0 + h0} 后 {s1 + h1}")

print("== 转移速率随流量变化 ==")
_, hh_full = step(config.SIM_LEVEL_FLOW_REF, 50.0, 20.0, 1.0)
_, hh_half = step(config.SIM_LEVEL_FLOW_REF / 2, 50.0, 20.0, 1.0)
full_move, half_move = hh_full - 20.0, hh_half - 20.0
check("半速流量转移量约为满速一半", abs(half_move - full_move / 2) < 0.02,
      f"half={half_move:.4f} full={full_move:.4f}")
_, hh_cap = step(config.SIM_LEVEL_FLOW_REF * 10, 50.0, 20.0, 1.0)
check("超基准流量时速率封顶(不超过2倍)", abs((hh_cap - 20.0) - full_move * 2) < 0.02,
      f"cap={hh_cap - 20.0:.4f} full={full_move:.4f}")

print("== 储水槽最低水位保护（防抽干）==")
s, h = step(config.SIM_LEVEL_FLOW_REF, config.SIM_LEVEL_STORAGE_MIN + 0.05, 30.0, 5.0)
print(f"  5s 后 储水槽 {s:.4f}%  加热槽 {h:.4f}%")
check("储水槽不会被抽到最低水位以下", s >= config.SIM_LEVEL_STORAGE_MIN - 1e-9, str(s))
check("抽到下限后停止转移", abs(s - config.SIM_LEVEL_STORAGE_MIN) < 1e-6, str(s))

print("== 加热槽满溢保护 ==")
s, h = step(config.SIM_LEVEL_FLOW_REF, 60.0, 99.95, 5.0)
check("加热槽不会超过 100%", h <= 100.0, str(h))
check("储水槽不会因此变成负值", s >= 0.0, str(s))

print("== 停机后两槽缓慢回平 ==")
s0, h0 = 80.0, 20.0
s1, h1 = step(0.0, s0, h0, 1.0)
print(f"  1s 后 储水槽 {s1:.4f}%  加热槽 {h1:.4f}%")
check("水位差缩小", (s1 - h1) < (s0 - h0), f"差 {s0 - h0} -> {s1 - h1}")
check("两槽仍守恒", abs((s1 + h1) - (s0 + h0)) < 1e-6, "")
check("回平方向正确（高者降、低者升）", s1 < s0 and h1 > h0, f"{s1:.3f},{h1:.3f}")

print("== 长暂停保护（单步 dt 不超过 2s）==")
s, h = step(config.SIM_LEVEL_FLOW_REF, 60.0, 40.0, 3600.0)
check("长时间暂停后不会一步抽干/灌满",
      abs((60.0 - s) - config.SIM_LEVEL_TRANSFER_PER_S * 2) < 0.02, str(s))

print("== 边界钳制 ==")
s, h = step(0.0, 0.0, 0.0, 1.0)
check("下限钳制到 0%", s == 0.0 and h == 0.0, f"{s},{h}")

print("== 来源标记与接口字段 ==")
st = plant.status()
check("status() 暴露两槽液位", {"level_storage", "level_heater"} <= set(st.keys()), "")
check("两槽液位均在 0~100", 0.0 <= st["level_storage"] <= 100.0 and 0.0 <= st["level_heater"] <= 100.0,
      f'{st["level_storage"]},{st["level_heater"]}')
check("两槽液位通道默认未接入",
      config.DEVICE_FEATURES["level_storage"] is False and config.DEVICE_FEATURES["level_heater"] is False,
      str(config.DEVICE_FEATURES))
check("TANKS 配置为双水槽", tuple(config.TANKS) == ("storage", "heater"), str(config.TANKS))

print("\n结果: 全部通过" if bad == 0 else f"\n结果: {bad} 项失败")
sys.exit(1 if bad else 0)
