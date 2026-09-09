"""组委会智能判定服务对接（任务五）：数据上报 / 指令轮询 / 执行结果反馈。

HTTP+JSON，使用 urllib 标准库(无第三方依赖，可离线)。判定服务地址与路径
在 config.py 用【现场修改】标注集中填写；接口字段不符时在此集中修改。
未启用或未填地址时为空操作，不影响本地系统。
"""
import json
import threading
import time
import urllib.error
import urllib.request

import config
import store


def _post(path: str, payload: dict) -> dict | None:
    """向判定服务发送 JSON，返回响应 dict；失败返回 None。"""
    url = config.JUDGE_URL.rstrip("/") + path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


class JudgeService:
    """判定服务客户端：记录最近通信结果与收到的控制指令，供前端展示。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.log: list[dict] = []     # 最近通信记录（时间/动作/结果）
        self.pending_command = None   # 最近收到的控制指令 {"action":..,"device":..}
        self.last_report = ""
        self.last_poll = ""

    def enabled(self) -> bool:
        return bool(store.judge_enabled() and config.JUDGE_URL)

    def _append_log(self, action: str, ok: bool, detail: str = "") -> None:
        with self._lock:
            self.log.insert(0, {
                "time": time.strftime("%H:%M:%S"),
                "action": action,
                "ok": ok,
                "detail": detail,
            })
            self.log = self.log[:50]

    def report(self, data: dict) -> None:
        """按周期上报全部传感数据与设备运行状态。"""
        if not self.enabled():
            return
        ok = _post(config.JUDGE_REPORT_PATH, {
            "device_id": config.JUDGE_DEVICE_ID,
            "data": {
                "storage_temp": data.get("storage_temp"),
                "heater_temp": data.get("heater_temp"),
                "flow_rate": data.get("flow_rate"),
                "pressure": data.get("pressure"),
                "pump_state": data.get("pump_state"),
                "heater_state": data.get("heater_state"),
            },
        })
        self._append_log("上报", ok is not None)
        self.last_report = time.strftime("%H:%M:%S")

    def poll(self) -> None:
        """轮询控制指令；收到后存入 pending_command 交给业务层执行。"""
        if not self.enabled():
            return
        ok = _post(config.JUDGE_POLL_PATH, {"device_id": config.JUDGE_DEVICE_ID})
        if ok is not None:
            cmd = ok.get("command") or ok.get("data")
            if cmd:
                with self._lock:
                    self.pending_command = cmd
        self._append_log("指令轮询", ok is not None)
        self.last_poll = time.strftime("%H:%M:%S")

    def feedback(self, result: str, detail: dict | None = None) -> None:
        """上报某条控制指令的执行结果与最新状态。"""
        if not self.enabled():
            return
        ok = _post(config.JUDGE_FEEDBACK_PATH, {
            "device_id": config.JUDGE_DEVICE_ID,
            "result": result,
            "detail": detail or {},
        })
        self._append_log("结果反馈", ok is not None)

    def take_command(self) -> dict:
        """取走并清空待执行指令(业务层执行后调用 feedback)。"""
        with self._lock:
            cmd = self.pending_command
            self.pending_command = None
            return cmd or {}

    def status(self) -> dict:
        """供 /api/water/judge/status 返回的信息。"""
        with self._lock:
            return {
                "enabled": self.enabled(),
                "url": config.JUDGE_URL,
                "device_id": config.JUDGE_DEVICE_ID,
                "last_report": self.last_report,
                "last_poll": self.last_poll,
                "pending_command": self.pending_command,
                "log": list(self.log),
            }