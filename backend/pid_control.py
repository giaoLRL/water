"""本地恒温闭环控制(PID)（任务六）：以加热槽温度为被控量，PID输出占空比→加热启停。

实现简单、直观：每周期调用 update(目标, 实测) 得到 0~100% 占空比，
heater_action() 把占空比≥50% 映射为加热器「开」，否则「关」，配合周期控制即可稳态±1℃。
"""
import config


class PID:
    """位置式PID：kp比例 / ki积分 / kd微分。"""

    def __init__(self, kp: float, ki: float, kd: float):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.integral = 0.0
        self.last_error = 0.0
        self.last_duty = 0.0   # 最近一次占空比(0~100%)，供界面显示

    def update(self, setpoint: float, measured: float, dt: float) -> float:
        """按 (目标-实测) 误差更新PID，返回占空比 0~100%。"""
        error = setpoint - measured
        self.integral += error * dt
        # 积分限幅，防止长时间误差造成积分饱和
        self.integral = max(-200.0, min(200.0, self.integral))
        d_out = (error - self.last_error) / dt if dt > 0 else 0.0
        duty = self.kp * error + self.ki * self.integral + self.kd * d_out
        duty = max(0.0, min(100.0, duty))
        self.last_error = error
        self.last_duty = duty
        return duty

    def reset(self) -> None:
        """切换到目标温度或手动模式时清零积分，避免过冲。"""
        self.integral = 0.0
        self.last_error = 0.0
        self.last_duty = 0.0


def heater_action(duty: float) -> str:
    """占空比→加热器开关：≥50% 开，否则关(简单可读)。"""
    return "on" if duty >= 50 else "off"