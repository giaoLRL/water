"""灯杆采集端：每个灯杆是一个 LampDevice —— 传感器（ESP32 真实，无数据为 0）+ 视频流。

- HttpTempSensor: 从 ESP32（DHT11 + GY-302）HTTP 接口读温度/湿度/光照，带 TTL 缓存与失败熔断，
  设备掉线时快速返回无数据（对应数值为 0），避免拖慢采集循环；
- VideoStream: 配置了 RTSP 读真实视频流（失败自动降级模拟画面），否则渲染模拟监控画面；
- LampManager: 管理全部灯杆，并为每个灯杆启动持续人员识别器（PersonDetector）。
"""
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import cv2
import numpy as np

import config

# 强制 RTSP 使用 TCP 传输，避免网络抖动导致长时间卡死
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000|max_delay;500000",
)

_FRAME_WIDTH = 640
_FRAME_HEIGHT = 360


def render_sim_frame(lamp, light_on: bool) -> np.ndarray:
    """生成模拟监控画面（灯杆视角），灯开启时叠加光晕。"""
    h, w = _FRAME_HEIGHT, _FRAME_WIDTH
    frame = np.zeros((h, w, 3), dtype=np.uint8)

    # 天空渐变
    top = np.array([82, 108, 140], dtype=np.float32)
    bottom = np.array([24, 30, 44], dtype=np.float32)
    horizon = int(h * 0.62)
    for y in range(horizon):
        t = y / max(horizon - 1, 1)
        frame[y, :] = (top * (1 - t) + bottom * t).astype(np.uint8)

    # 地面
    frame[horizon:, :] = (58, 62, 70)

    # 建筑（深色矩形）
    rng = random.Random(lamp.id)
    building_color = (30, 36, 46)
    x = 0
    while x < w:
        bw = rng.randint(40, 90)
        bh = rng.randint(40, 140)
        cv2.rectangle(frame, (x, horizon - bh), (x + bw, horizon), building_color, -1)
        x += bw + rng.randint(6, 20)

    # 道路
    road_color = (48, 50, 56)
    cv2.rectangle(frame, (0, h - int(h * 0.22)), (w, h), road_color, -1)

    # 灯杆
    px = int(w * 0.5)
    top_y = int(h * 0.34)
    cv2.line(frame, (px, top_y), (px, h), (20, 24, 30), 5)
    cv2.line(frame, (px, top_y), (px + int(w * 0.12), top_y), (20, 24, 30), 4)
    light_center = (px + int(w * 0.12), top_y + 10)

    if light_on:
        # 光晕
        for r, alpha in [(46, 0.35), (32, 0.5), (20, 0.75)]:
            overlay = frame.copy()
            cv2.circle(overlay, light_center, r, (0, 220, 255), -1)
            cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)
        cv2.circle(frame, light_center, 9, (120, 240, 255), -1)
    else:
        cv2.circle(frame, light_center, 9, (90, 95, 105), -1)

    # 状态文字
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cv2.putText(frame, f"LAMP-{lamp.id}  {lamp.name}  {lamp.location}", (18, 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.62, (220, 230, 240), 1, cv2.LINE_AA)
    cv2.putText(frame, ts, (18, 54),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (190, 200, 210), 1, cv2.LINE_AA)
    state = "LIGHT ON" if light_on else "LIGHT OFF"
    color = (100, 240, 180) if light_on else (120, 130, 140)
    cv2.putText(frame, state, (18, 80),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)
    cv2.putText(frame, "SIMULATED FEED", (w - 190, h - 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (140, 150, 160), 1, cv2.LINE_AA)
    return frame


class VideoStream:
    """单灯杆视频流：有 RTSP 则读真实流（失败自动降级模拟），否则渲染模拟画面。"""

    def __init__(self, lamp: "LampDevice", rtsp_url: str):
        self.lamp = lamp
        self.rtsp_url = rtsp_url
        self.rtsp_enabled = bool(rtsp_url)
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._last_frame = 0.0
        self._running = True
        self.source = "sim"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def last_frame_age(self) -> float:
        with self._lock:
            return time.time() - self._last_frame

    def _run(self) -> None:
        if self.rtsp_url:
            self._rtsp_loop()
        else:
            self._sim_loop()

    def _rtsp_loop(self) -> None:
        cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            self.source = "sim"
            self._sim_loop()
            return
        self.source = "rtsp"
        while self._running:
            try:
                ok, frame = cap.read()
                if not ok or frame is None:
                    time.sleep(0.05)
                    continue
                frame = _resize(frame)
                with self._lock:
                    self._frame = frame
                    self._last_frame = time.time()
            except Exception:  # noqa: BLE001
                time.sleep(0.05)
        cap.release()

    def _sim_loop(self) -> None:
        while self._running:
            frame = render_sim_frame(self.lamp, self.lamp.snapshot()["light_state"] == "on")
            with self._lock:
                self._frame = frame
                self._last_frame = time.time()
            time.sleep(0.16)

    def get_frame(self) -> np.ndarray | None:
        with self._lock:
            return None if self._frame is None else self._frame.copy()


def _resize(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    if w > _FRAME_WIDTH:
        scale = _FRAME_WIDTH / w
        frame = cv2.resize(frame, (_FRAME_WIDTH, int(h * scale)))
    return frame


class HttpTempSensor:
    """从 ESP32 + DHT11 + GY-302 的 HTTP 接口读取温度/湿度/光照。

    带短 TTL 缓存，避免高频采样时频繁请求 ESP32；HTTP 请求失败返回 (None, False)，
    由 LampDevice 降级为模拟值。
    失败熔断：连续失败 _TRIP 次后进入冷却期（_COOLDOWN 秒内直接返回失败，不再发请求），
    避免设备掉线时每个采样周期都被阻塞满超时时间。
    接口约定：GET {url} 返回
    {"status":"ok","temperature":25.3,"humidity":60.2,"light":320.5,"unit":{...}}
    其中 light 在光照传感器（BH1750）读不到时为 null，但温湿度仍正常返回。
    """

    _TRIP = 3            # 连续失败次数达到该值触发熔断
    _COOLDOWN = 15.0     # 熔断冷却时长（秒）

    def __init__(self, url: str, timeout: float = 0.8, ttl: float = 3.0):
        self.url = url
        self.timeout = timeout
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict | None = None   # {"data": {...}, "ts": float}
        self._fail_count = 0
        self._cooldown_until = 0.0

    def read(self) -> tuple[dict | None, bool]:
        now = time.time()
        with self._lock:
            if self._cache and now - self._cache["ts"] < self.ttl:
                return self._cache["data"], True
            if now < self._cooldown_until:
                return None, False   # 熔断冷却期内快速失败，不发起网络请求
        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("status") not in ("ok", "partial"):
                raise ValueError(f"sensor status={payload.get('status')}")
            data = {
                "temperature": payload.get("temperature"),
                "humidity": payload.get("humidity"),
                "light": payload.get("light"),
            }
            with self._lock:
                self._cache = {"data": data, "ts": time.time()}
                self._fail_count = 0
            return data, True
        except (urllib.error.URLError, OSError, ValueError, KeyError, json.JSONDecodeError):
            with self._lock:
                self._fail_count += 1
                if self._fail_count >= self._TRIP:
                    self._cooldown_until = time.time() + self._COOLDOWN
                    self._fail_count = 0
            return None, False


class LampDevice:
    """单灯杆：传感器（真实 HTTP，无数据时为 0）+ 灯光（电磁阀）控制 + 视频流。"""

    def __init__(self, lamp: dict):
        self.id = lamp["id"]
        self.name = lamp["name"]
        self.location = lamp["location"]
        self.rtsp_url = lamp.get("rtsp_url", "")
        self._lock = threading.Lock()
        self._light_state = "off"
        # 无真实数据时数值为 0（不使用模拟数据）
        self._temperature = 0.0
        self._humidity = 0.0
        self._luminance = 0.0
        self.online = True
        # 真实温湿度/光照传感器（ESP32 HTTP 接口），未配置时为 None
        sensor_url = lamp.get("sensor_url", "") or ""
        self._http_sensor = HttpTempSensor(sensor_url) if sensor_url else None
        # None=未检测, True=在线, False=离线（真实传感器）
        self.sensor_online: bool | None = None   # ESP32 接口整体在线状态（温湿度）
        self.light_online: bool | None = None    # GY-302 光照传感器在线状态
        # 先占位，避免视频线程起来时 snapshot() 访问 self.video 报错
        self.video = None
        self.video = VideoStream(self, self.rtsp_url)

    def read_sensors(self) -> dict:
        """采样一次：仅当有真实传感器数据时使用真实值，否则对应指标直接为 0（不使用模拟数据）。"""
        with self._lock:
            temp = hum = lux = 0.0
            if self._http_sensor is not None:
                real, ok = self._http_sensor.read()
                self.sensor_online = ok
                light_ok = False
                if ok:
                    t = real.get("temperature")
                    h = real.get("humidity")
                    if t is not None:
                        temp = float(t)
                    if h is not None:
                        hum = max(0.0, min(100.0, float(h)))
                    l = real.get("light")
                    if l is not None:
                        lux = float(l)
                        light_ok = True
                self.light_online = light_ok
            else:
                self.sensor_online = False
                self.light_online = False
            self._temperature = temp
            self._humidity = hum
            self._luminance = lux
            return {
                "temperature": round(self._temperature, 2),
                "humidity": round(self._humidity, 2),
                "luminance": round(self._luminance, 1),
            }

    def set_light(self, action: str) -> None:
        if action not in ("on", "off"):
            raise ValueError("非法指令")
        with self._lock:
            self._light_state = action

    def snapshot(self) -> dict:
        video_online = True
        if self.video is not None:
            video_online = (
                not self.video.rtsp_enabled  # 模拟画面恒定在线
                or self.video.source == "rtsp" and self.video.last_frame_age < 6  # RTSP 正常出帧
                or self.video.source == "sim"  # 降级模拟也算有画面
            )
        with self._lock:
            return {
                "id": self.id,
                "name": self.name,
                "location": self.location,
                "temperature": round(self._temperature, 2),
                "humidity": round(self._humidity, 2),
                "luminance": round(self._luminance, 1),
                "light_state": self._light_state,
                "online": self.online,
                "sensor_source": "esp32" if self._http_sensor is not None else "sim",
                "sensor_online": self.sensor_online,
                "light_online": self.light_online,
                "video_source": self.video.source if self.video is not None else "sim",
                "video_online": video_online,
            }


class LampManager:
    """管理全部灯杆单元。"""

    def __init__(self):
        from detector import PersonDetector

        self._lamps = {lamp["id"]: LampDevice(lamp) for lamp in config.LAMP_POSTS}
        # 每个灯杆启动持续人员自动识别（生成标注视频流并按规则告警）
        self._detectors = {
            lamp_id: PersonDetector(lamp) for lamp_id, lamp in self._lamps.items()
        }

    def all(self) -> list[LampDevice]:
        return list(self._lamps.values())

    def get(self, lamp_id: str) -> LampDevice | None:
        return self._lamps.get(lamp_id)

    def detector(self, lamp_id: str):
        return self._detectors.get(lamp_id)