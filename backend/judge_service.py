"""组委会智能判定服务对接（任务五）：数据上报 / 指令轮询 / 执行结果反馈。

HTTP+JSON，使用 urllib 标准库(无第三方依赖，可离线)。判定服务地址与路径
在 config.py 用【现场修改】标注集中填写；**更推荐在系统配置页填报文模板**
（sys.judge_template）：现场拿到组委会的字段规范后，不用改代码，
直接改 URL/请求头/报文字段映射即可。

模板形如：
  {"url": "http://192.168.1.50:9000", "headers": {"X-Token": "abc"},
   "report":   {"path": "/api/report",
                "body": {"deviceId": "{{device_id}}", "ts": "{{ts}}",
                         "flow": "{{flow_rate}}", "raw": "{{data_json}}"}},
   "poll":     {"path": "/api/poll", "body": {"deviceId": "{{device_id}}"}},
   "feedback": {"path": "/api/feedback", "body": {"deviceId": "{{device_id}}", "ok": "{{result}}"}}}
未配置模板时使用下面的内置报文（与旧版行为一致），未启用或未填地址时为空操作。
"""
import json
import threading
import time
import urllib.error
import urllib.request

import config
import store


def judge_url() -> str:
    """判定服务地址：优先 sys.judge_url（界面可改），否则用 config.JUDGE_URL。"""
    return str(store.get("judge_url", "") or config.JUDGE_URL or "").rstrip("/")


def load_template() -> dict:
    """读取报文模板（未配置返回空 dict）。"""
    try:
        raw = store.get_json("judge_template", {})
    except Exception:  # noqa: BLE001
        raw = {}
    return raw if isinstance(raw, dict) else {}


def render(node, data: dict):
    """模板占位符替换：字符串中的 {{key}} 用 data[key] 替换（缺失→空字符串）。

    {{data_json}} 特殊：展开为整个快照（便于一次性上报全部字段）。
    支持嵌套 dict / list。
    """
    import re
    if isinstance(node, dict):
        return {k: render(v, data) for k, v in node.items()}
    if isinstance(node, list):
        return [render(v, data) for v in node]
    if isinstance(node, str):
        def sub(m):
            key = m.group(1).strip()
            if key == "data_json":
                return json.dumps(data, ensure_ascii=False, default=str)
            value = data.get(key)
            return "" if value is None else str(value)
        whole = re.fullmatch(r"\{\{\s*([^}]+?)\s*\}\}", node)
        if whole and whole.group(1).strip() != "data_json":
            return data.get(whole.group(1).strip())      # 整串占位时保留原始类型（数字/布尔）
        return re.sub(r"\{\{\s*([^}]+?)\s*\}\}", sub, node)
    return node


def _post(path: str, payload: dict, headers: dict | None = None) -> dict | None:
    """向判定服务发送 JSON，返回响应 dict；失败返回 None。"""
    base = judge_url()
    if not base:
        return None
    url = base + path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    head = {"Content-Type": "application/json"}
    head.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=head)
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
        return bool(store.judge_enabled() and judge_url())

    def _template(self) -> dict:
        return load_template()

    def _send(self, kind: str, data: dict, builtin_path: str, builtin_body: dict,
              fallback_fields: dict | None = None) -> dict | None:
        """按模板发送；未配置该段模板时退回内置报文。"""
        tpl = self._template()
        headers = tpl.get("headers") if isinstance(tpl.get("headers"), dict) else None
        section = tpl.get(kind) if isinstance(tpl.get(kind), dict) else None
        if section and section.get("path"):
            merged = dict(data)
            if fallback_fields:
                merged.update(fallback_fields)      # result 等运行时字段也参与替换
            body = render(section.get("body") or {}, merged)
            return _post(str(section["path"]), body, headers)
        if fallback_fields:
            builtin_body = dict(builtin_body)
            builtin_body.update(fallback_fields)
        return _post(builtin_path, builtin_body, headers)

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
        snapshot = dict(data)
        snapshot.setdefault("device_id", config.JUDGE_DEVICE_ID)
        snapshot.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        ok = self._send("report", snapshot, config.JUDGE_REPORT_PATH, {
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
        snapshot = {"device_id": config.JUDGE_DEVICE_ID,
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        ok = self._send("poll", snapshot, config.JUDGE_POLL_PATH,
                        {"device_id": config.JUDGE_DEVICE_ID})
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
        snapshot = {"device_id": config.JUDGE_DEVICE_ID, "result": result,
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    **(detail or {})}
        ok = self._send("feedback", snapshot, config.JUDGE_FEEDBACK_PATH, {
            "device_id": config.JUDGE_DEVICE_ID,
            "result": result,
            "detail": detail or {},
        }, fallback_fields={"result": result})
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
                "url": judge_url(),
                "device_id": config.JUDGE_DEVICE_ID,
                "template": bool(self._template()),
                "last_report": self.last_report,
                "last_poll": self.last_poll,
                "pending_command": self.pending_command,
                "log": list(self.log),
            }
