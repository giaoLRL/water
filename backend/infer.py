"""智能识别服务辅助：封装的帧 <-> base64 dataURL 转换与 YOLO 推理接口调用。

供 api.py（手动截图检测）与 detector.py（持续自动识别）复用，
统一图片编解码与 POST /infer 请求逻辑，避免多处重复实现。
"""
import base64
import json
import urllib.request

import cv2
import numpy as np

import store


def frame_to_dataurl(frame) -> str:
    ok, buf = cv2.imencode(".jpg", frame)
    if not ok:
        raise ValueError("图片编码失败")
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("utf-8")


def dataurl_to_frame(dataurl):
    """把带 data: 前缀的 base64 图片解码为 OpenCV 帧；失败返回 None。"""
    if not dataurl:
        return None
    try:
        b64 = dataurl.split(",", 1)[1]
        buf = np.frombuffer(base64.b64decode(b64), dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        return None


def call_infer(image_dataurl: str) -> dict:
    """调用人员智能识别接口，返回原始 JSON。地址/超时可在系统配置页调整（每轮读取）。"""
    body = json.dumps({"image": image_dataurl}).encode("utf-8")
    req = urllib.request.Request(
        store.infer_url(),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=store.infer_timeout()) as resp:
        return json.loads(resp.read().decode("utf-8"))