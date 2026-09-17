r"""液位通道契约单元测试：无模拟数据，没有数据一律保持 0。

背景：用户要求清除全部模拟水位。当前固件只提供一路超声波液位（接加热槽），
因此：
  - 储水槽（level_storage=False，无传感器路径）水位恒为 0；
  - 加热槽（level_heater=True）读 /api/level 实测值，读数无效（503/404）时归 0；
  - 设备持续离线后两槽水位统一归 0。
本测试不访问网络与数据库，可独立运行：
    python backend/tests/test_level.py
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

print("== 初始状态：无任何预置水位（不存在模拟初值）==")
check("新建实例两槽水位均为 0", plant.level_storage == 0.0 and plant.level_heater == 0.0,
      f"{plant.level_storage},{plant.level_heater}")
check("不再持有模拟计时器 _last_level_ts", not hasattr(plant, "_last_level_ts"), "")
check("不再存在模拟方法 _simulate_levels",
      not hasattr(plant, "_simulate_levels"), "")

print("== 未接入传感器的槽位（储水槽）恒为 0 ==")
plant.level_storage = 88.0                     # 人为置脏，验证会被归 0 而不是保留
plant._read_level("storage")                   # 该槽 LEVEL_PATH 为空 → 直接归 0
check("未配置传感器路径 → 归 0", plant.level_storage == 0.0, str(plant.level_storage))
check("储水槽功能位为 False", config.DEVICE_FEATURES["level_storage"] is False,
      str(config.DEVICE_FEATURES))
check("储水槽传感器路径为空", config.LEVEL_PATH_STORAGE == "", repr(config.LEVEL_PATH_STORAGE))

print("== 配置中不再残留模拟参数 ==")
leftover = [k for k in dir(config) if k.startswith("SIM_")]
check("config 无 SIM_* 模拟参数残留", leftover == [], str(leftover))

print("== 设备持续离线：两槽水位统一归 0 ==")
plant.level_storage, plant.level_heater = 42.0, 77.0
plant._last_ok_ts = time.time() - config.DEVICE_OFFLINE_AFTER_S - 1   # 模拟已超过离线判定时长
plant._mark_failed("无法连接设备")
check("离线后储水槽归 0", plant.level_storage == 0.0, str(plant.level_storage))
check("离线后加热槽归 0", plant.level_heater == 0.0, str(plant.level_heater))
check("离线状态已标记", plant.sensor_online is False, str(plant.sensor_online))

print("== status() 暴露字段与取值范围 ==")
st = plant.status()
check("status() 暴露两槽液位", {"level_storage", "level_heater"} <= set(st.keys()), "")
check("两槽液位均在 0~100", 0.0 <= st["level_storage"] <= 100.0 and 0.0 <= st["level_heater"] <= 100.0,
      f'{st["level_storage"]},{st["level_heater"]}')
check("加热槽功能位为 True(已接超声波)", config.DEVICE_FEATURES["level_heater"] is True,
      str(config.DEVICE_FEATURES))
check("加热槽传感器路径为 /api/level", config.LEVEL_PATH_HEATER == "/api/level",
      repr(config.LEVEL_PATH_HEATER))
check("TANKS 配置为双水槽", tuple(config.TANKS) == ("storage", "heater"), str(config.TANKS))

print("\n结果: 全部通过" if bad == 0 else f"\n结果: {bad} 项失败")
sys.exit(1 if bad else 0)