"""
Screen Capture Thread — continuously captures the iPhone screen and emits QImage
frames to the GUI thread.

Prefers the USB video stream (~60 FPS, see video_stream.py) and falls back to DVT
screenshots, captured over several channels in parallel to multiply their throughput.
"""

import itertools
import logging
import threading
import time

from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage

try:
    import video_stream
except ImportError as e:  # pyobjc not installed (or not macOS)
    video_stream = None
    video_stream_import_error = e

logger = logging.getLogger(__name__)

# Each DVT channel delivers ~5 screenshots/s; the device serves channels independently,
# so throughput scales roughly linearly with the channel count.
DVT_CHANNELS = 4


def enable_video_capture():
    """Prepare the USB video stream. Call once on the main thread at startup."""
    if video_stream is None:
        return
    try:
        video_stream.enable_screen_capture_devices()
    except Exception as e:
        logger.warning(f"Could not enable USB video devices: {e}")


class ScreenCaptureThread(QThread):
    """Background thread that continuously captures the iPhone screen."""

    frame_ready = pyqtSignal(QImage)
    fps_updated = pyqtSignal(float)
    capture_error = pyqtSignal(str)
    mode_changed = pyqtSignal(str)

    _frame_pending = pyqtSignal()

    def __init__(self, device_manager):
        super().__init__()
        self._device_manager = device_manager
        self._running = False

        # Only the newest frame is kept, so a busy GUI thread drops frames instead of queueing them
        self._frame_lock = threading.Lock()
        self._latest_frame = None
        self._latest_seq = -1
        self._frame_count = 0
        self._seq = itertools.count()
        self._frame_pending.connect(self._emit_latest_frame)

    def run(self):
        self._running = True
        logger.info("Screen capture started")
        if not self._run_video_stream() and self._running:
            self._run_dvt()
        logger.info("Screen capture stopped")

    def stop(self):
        self._running = False
        self.wait(5000)

    # --- Frame delivery (any thread -> GUI thread) ---

    def _deliver(self, image: QImage, seq: int):
        with self._frame_lock:
            if seq < self._latest_seq:
                # A parallel DVT request started earlier finished later — drop the stale frame
                return
            self._latest_seq = seq
            self._frame_count += 1
            notify = self._latest_frame is None
            self._latest_frame = image
        if notify:
            self._frame_pending.emit()

    @pyqtSlot()
    def _emit_latest_frame(self):
        """Runs on the GUI thread."""
        with self._frame_lock:
            image, self._latest_frame = self._latest_frame, None
        if image is not None and self._running:
            self.frame_ready.emit(image)

    def _report_fps_until_stopped(self, keep_going=lambda: True):
        """Emit the FPS once per second until stop() is called or keep_going() returns False."""
        fps_timer = time.monotonic()
        while self._running and keep_going():
            time.sleep(0.1)
            elapsed = time.monotonic() - fps_timer
            if elapsed >= 1.0:
                with self._frame_lock:
                    frames, self._frame_count = self._frame_count, 0
                self.fps_updated.emit(frames / elapsed)
                fps_timer = time.monotonic()

    # --- USB video stream ---

    def _run_video_stream(self) -> bool:
        """Stream via AVFoundation. Returns False if it can't start, so the caller can fall back to DVT."""
        if video_stream is None:
            logger.info(f"USB video stream unavailable ({video_stream_import_error}) — using DVT screenshots")
            return False

        name = self._device_manager.device_info.get('name')
        try:
            device = video_stream.find_device(name)
            if device is None:
                logger.info("iPhone not found as USB video device — using DVT screenshots")
                return False
            stream = video_stream.VideoStream(device, lambda image: self._deliver(image, next(self._seq)))
            stream.start()
        except Exception as e:
            logger.warning(f"USB video stream failed ({e}) — using DVT screenshots")
            return False
        if not stream.is_running:
            logger.warning("USB video stream did not start (device busy?) — using DVT screenshots")
            return False

        # No falling back to DVT from here on: the stream switches the iPhone's USB mode, which
        # drops the developer tunnel for a while. Frames start ~5 s after the first start and
        # pause while the iPhone display is off.
        logger.info(f"Capturing via USB video stream from {device.localizedName()}")
        self.mode_changed.emit("USB video")
        try:
            self._report_fps_until_stopped(keep_going=lambda: stream.is_running)
            if self._running:
                self._fail("USB video stream stopped")
        finally:
            stream.stop()
        return True

    # --- DVT screenshots ---

    def _run_dvt(self):
        channels = self._device_manager.open_screenshot_channels(DVT_CHANNELS)
        if channels == 0:
            self._fail("No screen source: the USB stream isn't available and the screenshot fallback "
                       "only works up to iOS 16 — see the Doctor tab")
            return
        logger.info(f"Capturing via DVT screenshots ({channels} parallel channels)")
        self.mode_changed.emit("screenshots")

        workers = [
            threading.Thread(target=self._dvt_worker, args=(channel,), daemon=True)
            for channel in range(max(channels, 1))
        ]
        for worker in workers:
            worker.start()
        self._report_fps_until_stopped()
        for worker in workers:
            worker.join(timeout=2)

    def _dvt_worker(self, channel: int):
        consecutive_errors = 0
        while self._running:
            try:
                seq = next(self._seq)
                # Capture screenshot (returns PNG bytes) — the bottleneck, ~100 ms per request
                screenshot_bytes = self._device_manager.take_screenshot(channel)

                qimage = QImage()
                if not qimage.loadFromData(screenshot_bytes):
                    continue
                self._deliver(qimage, seq)
                consecutive_errors = 0

            except ConnectionError as e:
                logger.error(f"Connection error in capture: {e}")
                self._fail(str(e))

            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= 10:
                    self._fail(f"Too many capture errors: {e}")
                # Brief backoff
                time.sleep(0.5)

    def _fail(self, message: str):
        # Parallel workers usually fail together — report only the first error
        with self._frame_lock:
            was_running, self._running = self._running, False
        if was_running:
            self.capture_error.emit(message)
