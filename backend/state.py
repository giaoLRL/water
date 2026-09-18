"""全局运行时状态：共享设备 / 告警 / PID / 判定服务实例。

main、采集循环、API 通过 state.services 访问同一组实例，避免重复初始化。
保持简单：不引入复杂容器，只挂载各单例。
"""
import time

import config


class Services:
    def __init__(self):
        self.plant = None      # WaterPlant 水循环设备
        self.alarm = None      # AlarmEngine 告警引擎
        self.pid = None        # PID 恒温控制器
        self.pid_loops = None  # PidLoopManager 恒温闭环回路（卡片即来源，可多路）
        self.judge = None      # JudgeService 判定服务
        self.gateway = None    # ModbusGateway 外部设备（Modbus/串口）接入适配器
        self.timers = None     # TimerScheduler 定时任务调度
        self.ingest = {}       # 采集端最新上报数据(用于真实链路)
        self.start_time = time.time()

    @property
    def uptime(self) -> float:
        return time.time() - self.start_time


services = Services()
