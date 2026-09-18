"""本地模拟"智能判定服务"：用组委会给的接口规范联调前，先用它自测上报/轮询/反馈。

接口（可用 --prefix 改前缀）：
    POST {prefix}/api/report    记录上报数据，返回 {"code":0}
    POST {prefix}/api/poll      返回下一条待下发指令（--cmd 可预置，如 pump:off）
    POST {prefix}/api/feedback  记录执行结果
    GET  {prefix}/stats         返回计数与最近记录（自测用）

用法：
    python backend/tests/mock_judge.py --port 9100
    python backend/tests/mock_judge.py --port 9100 --cmd "pump:off"     # 轮询时下发一次关泵
"""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.reports: list[dict] = []
        self.polls = 0
        self.feedbacks: list[dict] = []
        self.commands: list[dict] = []


def make_handler(prefix: str, state: _State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MockJudge/1.0"

        def log_message(self, *args):        # 静默，避免刷屏
            return

        def _json(self, obj, code: int = 200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/") == prefix + "/stats":
                with state.lock:
                    self._json({"reports": len(state.reports), "polls": state.polls,
                                "feedbacks": len(state.feedbacks),
                                "pending_commands": len(state.commands),
                                "last_report": state.reports[-1] if state.reports else None,
                                "last_feedback": state.feedbacks[-1] if state.feedbacks else None})
            else:
                self._json({"code": 40004, "msg": "not found"}, 404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001
                payload = {"_raw": raw.decode("utf-8", "replace")}
            path = self.path.split("?")[0].rstrip("/")
            if path == prefix + "/api/report":
                with state.lock:
                    state.reports.append(payload)
                self._json({"code": 0, "msg": "ok"})
            elif path == prefix + "/api/poll":
                with state.lock:
                    state.polls += 1
                    cmd = state.commands.pop(0) if state.commands else None
                self._json({"code": 0, "command": cmd})
            elif path == prefix + "/api/feedback":
                with state.lock:
                    state.feedbacks.append(payload)
                self._json({"code": 0, "msg": "ok"})
            else:
                self._json({"code": 40004, "msg": "not found"}, 404)

    return Handler


def start(port: int = 9100, prefix: str = "", commands: list[dict] | None = None):
    """启动模拟服务（后台线程），返回 (server, state)。"""
    state = _State()
    state.commands = list(commands or [])
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(prefix, state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="模拟智能判定服务")
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--prefix", default="")
    ap.add_argument("--cmd", default="", help='预置一条指令，如 "pump:off"（可多次）')
    args = ap.parse_args()
    cmds = []
    for item in [c for c in [args.cmd] if c]:
        device, _, action = item.partition(":")
        cmds.append({"device": device, "action": action or "off"})
    srv, state = start(args.port, args.prefix, cmds)
    print(f"模拟判定服务已启动: http://127.0.0.1:{args.port}{args.prefix or ''}（Ctrl+C 停止）")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
        print("已停止")
