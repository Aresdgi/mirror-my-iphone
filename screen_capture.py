"""
Screen Capture Thread — continuously captures iPhone screenshots via DVT
and emits QImage frames to the GUI thread.
"""

import logging
import time

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage

logger = logging.getLogger(__name__)


class ScreenCaptureThread(QThread):
    """Background thread that continuously captures screenshots from the iPhone."""

    frame_ready = pyqtSignal(QImage)
    fps_updated = pyqtSignal(float)
    capture_error = pyqtSignal(str)

    def __init__(self, device_manager, target_fps: int = 30):
        super().__init__()
        self._device_manager = device_manager
        self._running = False
        self._target_fps = target_fps
        self._paused = False

    @property
    def target_fps(self) -> int:
        return self._target_fps

    @target_fps.setter
    def target_fps(self, value: int):
        self._target_fps = max(1, min(60, value))

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def run(self):
        """Main capture loop — runs as fast as DVT allows."""
        self._running = True
        frame_count = 0
        fps_timer = time.time()
        consecutive_errors = 0

        logger.info(f"Screen capture started (target: {self._target_fps} FPS)")

        while self._running:
            if self._paused:
                time.sleep(0.1)
                continue

            try:
                # Capture screenshot (returns PNG bytes) — this is the bottleneck
                screenshot_bytes = self._device_manager.take_screenshot()

                # Convert PNG bytes directly to QImage
                qimage = QImage()
                if not qimage.loadFromData(screenshot_bytes):
                    continue

                # Emit to main thread (Qt handles the cross-thread copy)
                self.frame_ready.emit(qimage)

                # FPS tracking
                frame_count += 1
                elapsed = time.time() - fps_timer
                if elapsed >= 1.0:
                    self.fps_updated.emit(frame_count / elapsed)
                    frame_count = 0
                    fps_timer = time.time()

                consecutive_errors = 0

                # No sleep — DVT screenshot is already the bottleneck (~100-300ms per frame)
                # The target_fps limiter was actually slowing us down further

            except ConnectionError as e:
                consecutive_errors += 1
                logger.error(f"Connection error in capture: {e}")
                self.capture_error.emit(str(e))
                self._running = False
                break

            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= 10:
                    self.capture_error.emit(f"Zu viele Fehler: {e}")
                    self._running = False
                    break
                # Brief backoff
                time.sleep(0.5)

        logger.info("Screen capture stopped")

    def stop(self):
        self._running = False
        self.wait(5000)
