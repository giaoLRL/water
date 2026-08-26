"""设备健康检测：按接口可达性定期探测数据库、AI 识别服务等设备在线状态。"""
import threading
import time
import urllib.error
import urllib.request

import config


def _probe_url(url: str, timeout: float = 1.5) -> bool:
    """探测 HTTP 接口是否可达：连接成功即视为在线（即使返回 4xx/5xx）。"""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001
        return False


class DeviceMonitor:
    """后台线程每 10 秒按接口状态刷新一次设备健康信息。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._db_online: bool | None = None
        self._infer_online: bool | None = None
        self._last_check: float = 0.0
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self._check_once()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(10)

    def _check_once(self) -> None:
        import database

        db_ok = database.ping()
        infer_ok = _probe_url(config.INFER_URL)
        with self._lock:
            self._db_online = db_ok
            self._infer_online = infer_ok
            self._last_check = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "database": self._db_online,
                "infer": self._infer_online,
                "checked_at": self._last_check,
            }