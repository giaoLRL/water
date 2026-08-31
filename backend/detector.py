"""灯杆人员持续自动识别：每个灯杆一个后台线程，周期截图 -> YOLO 识别 ->
更新标注帧（供标注视频流播放）-> 按人数规则触发 / 恢复告警。

- 识别频率可在"系统配置"页调整（store.person_detect_interval，默认 5 秒/灯杆）；
- 识别到人时写入人员监测记录（含原始图与标注图），无人则不落库以免刷爆数据库；
- 标注帧通过 /detect_video 接口实时播放，人数与告警状态通过 /detect/current 查询。
"""
import threading
import time
from datetime import datetime

import cv2

import database
import infer
import store
from state import services


class PersonDetector:
    """单灯杆持续人员识别器。"""

    def __init__(self, lamp):
        self.lamp = lamp
        self._lock = threading.Lock()
        self._labeled_frame = None   # 最近一次标注帧（np.ndarray），用于标注视频流
        self._boxes = []             # 最近一次识别结果（含 bbox），供本地实时画框
        self._last_result = None     # 最近一次识别结果摘要
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止识别线程（供灯杆配置变更重建时调用）。"""
        self._running = False

    def _loop(self) -> None:
        while self._running:
            try:
                self._run_once()
            except Exception:  # noqa: BLE001
                pass
            # 每轮重读识别间隔，改配置即时生效
            time.sleep(store.person_detect_interval())

    def _run_once(self) -> None:
        frame = self.lamp.video.get_frame()
        if frame is None:
            return
        original = infer.frame_to_dataurl(frame)
        result = infer.call_infer(original)
        inference_results = result.get("inference_results", []) or []
        processed = result.get("processed_image")
        # 保存最新识别框（含像素坐标），供标注视频流在每一帧上本地实时绘制
        with self._lock:
            self._boxes = list(inference_results)
        person_count = len(inference_results)
        max_conf = max((float(i.get("confidence", 0)) for i in inference_results), default=0.0)
        ts = datetime.now()
        labeled = infer.dataurl_to_frame(processed)

        # 只记录有人出现的识别点，避免无人空帧反复刷库
        if person_count > 0:
            database.insert_detection(
                self.lamp.id, ts, person_count, round(max_conf, 4), original, processed,
            )

        with self._lock:
            self._labeled_frame = labeled
            self._last_result = {
                "lamp_id": self.lamp.id,
                "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "person_count": person_count,
                "max_confidence": round(max_conf, 4),
                "alarm_active": False,
            }

        # 人数告警：走统一告警引擎，规则（启用 + 人数阈值）可在前端自定义；携带 YOLO 标注图作为快照
        alarm = services.alarm
        if alarm is None:
            return
        active = alarm.check_person(self.lamp.id, person_count, image=processed)
        with self._lock:
            if self._last_result is not None:
                self._last_result["alarm_active"] = any(a["type"] == "person" for a in active)

    def get_labeled_frame(self):
        with self._lock:
            return None if self._labeled_frame is None else self._labeled_frame.copy()

    def annotate(self, frame):
        """在给定帧上绘制最新识别框（本地绘制，毫秒级），供标注视频流全帧率实时使用。"""
        with self._lock:
            boxes = list(self._boxes)
        if not boxes:
            return frame
        annotated = frame.copy()
        for item in boxes:
            bb = item.get("bbox")
            if not bb or len(bb) != 4:
                continue
            x1, y1, x2, y2 = [int(v) for v in bb]
            conf = float(item.get("confidence", 0))
            label = item.get("label") or "person"
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(annotated, f"{label} {conf:.2f}", (x1, max(0, y1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        return annotated

    def last_result(self) -> dict | None:
        with self._lock:
            return dict(self._last_result) if self._last_result else None
