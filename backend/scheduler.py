"""定时任务调度（简单 cron 式）：到点执行若干"卡片动作"，与报警联动同一套执行器。

配置存 config 表 sys.timers（JSON 数组），系统配置页编辑：
  {id, name, enabled, mode: "interval"|"daily",
   every_sec: 600,            # interval 模式：每 N 秒执行一次（≥5）
   at: "08:00",               # daily 模式：每天该时刻执行一次
   actions: [{card, state, kind, url}]}   # 与联动动作同构（卡片即来源）

执行动作复用 AlarmEngine 的动作解析与审计（tag="timer" → 日志 action=timer_*，source=timer）。
"""
import threading
import time
from datetime import datetime

import store

MIN_INTERVAL_SEC = 5


def load_timers() -> list[dict]:
    """读取定时任务配置（无配置返回空列表）。"""
    try:
        items = store.get_json("timers", [])
    except Exception:  # noqa: BLE001
        items = []
    return [t for t in (items if isinstance(items, list) else [])
            if isinstance(t, dict) and t.get("id")]


class TimerScheduler:
    """采集循环每周期调用 tick()，内部按各任务计划判断是否到点。"""

    def __init__(self, alarm=None):
        self._lock = threading.Lock()
        self._timers: list[dict] = []
        self._last: dict[str, float] = {}      # interval: 上次执行时间戳
        self._last_day: dict[str, str] = {}    # daily: 上次执行的日期
        self.alarm = alarm                     # 执行动作借用 AlarmEngine（动作解析/审计）
        self.reload()

    def reload(self) -> None:
        """重新加载任务；新任务的计时从当前时刻开始（不会一保存就立刻触发）。"""
        now = time.time()
        with self._lock:
            self._timers = [t for t in load_timers() if t.get("enabled", True)]
            for t in self._timers:
                self._last.setdefault(str(t["id"]), now)
            ids = {str(t["id"]) for t in self._timers}
            self._last = {k: v for k, v in self._last.items() if k in ids}

    def timers(self) -> list[dict]:
        with self._lock:
            return list(self._timers)

    def tick(self) -> None:
        """检查是否有任务到点；到期则执行其动作（失败只记日志，不影响采集循环）。"""
        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        hhmm = datetime.now().strftime("%H:%M")
        due: list[dict] = []
        with self._lock:
            for t in self._timers:
                tid = str(t["id"])
                if t.get("mode") == "daily":
                    if str(t.get("at") or "") == hhmm and self._last_day.get(tid) != today:
                        self._last_day[tid] = today
                        due.append(t)
                else:
                    try:
                        every = max(MIN_INTERVAL_SEC, float(t.get("every_sec") or 600))
                    except (TypeError, ValueError):
                        every = 600.0
                    if now - self._last.get(tid, now) >= every:
                        self._last[tid] = now
                        due.append(t)
        for t in due:
            ctx = {"name": t.get("name") or t.get("id"), "value": 0.0, "threshold": 0.0,
                   "direction": "above", "text": "到点执行"}
            if self.alarm:
                self.alarm.run_actions(t.get("actions") or [], ctx, "定时", tag="timer", source="timer")

    def next_run(self, timer: dict):
        """返回下一次执行时间（仅用于界面展示，估算值）。"""
        tid = str(timer.get("id"))
        if timer.get("mode") == "daily":
            at = str(timer.get("at") or "")
            if len(at) == 5 and at[2] == ":":
                try:
                    hh, mm = int(at[:2]), int(at[3:])
                    now = datetime.now()
                    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if target <= now:
                        from datetime import timedelta
                        target = target + timedelta(days=1)
                    return target.strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return ""
            return ""
        try:
            every = max(MIN_INTERVAL_SEC, float(timer.get("every_sec") or 600))
        except (TypeError, ValueError):
            every = 600.0
        with self._lock:
            last = self._last.get(tid, time.time())
        return datetime.fromtimestamp(last + every).strftime("%Y-%m-%d %H:%M:%S")
