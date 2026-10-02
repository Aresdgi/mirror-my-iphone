"""
Video Stream — receives the iPhone screen as a live 60 FPS video stream over USB,
the same way QuickTime Player does (CoreMediaIO screen-capture device + AVFoundation).
Much faster than DVT screenshots and needs no developer tunnel. macOS only.
"""

import logging
import struct
import time

import objc
import AVFoundation
import CoreMedia
import CoreMediaIO
import Quartz
import libdispatch
from Foundation import NSObject
from PyQt6.QtGui import QImage

logger = logging.getLogger(__name__)


def enable_screen_capture_devices():
    """Make connected iOS devices show up as AVCaptureDevices (off by default).

    Must be called on the main thread: CoreMediaIO delivers device notifications to the
    run loop of the thread that first uses it, and only the main thread runs one.
    """
    address = CoreMediaIO.CMIOObjectPropertyAddress(
        CoreMediaIO.kCMIOHardwarePropertyAllowScreenCaptureDevices,
        CoreMediaIO.kCMIOObjectPropertyScopeGlobal,
        CoreMediaIO.kCMIOObjectPropertyElementMain,
    )
    allow = struct.pack('I', 1)
    CoreMediaIO.CMIOObjectSetPropertyData(
        CoreMediaIO.kCMIOObjectSystemObject, address, 0, objc.NULL, len(allow), allow
    )


def _ios_capture_devices() -> list:
    if hasattr(AVFoundation, 'AVCaptureDeviceTypeExternal'):  # macOS 14+
        devices = AVFoundation.AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
            [AVFoundation.AVCaptureDeviceTypeExternal],
            AVFoundation.AVMediaTypeMuxed,
            AVFoundation.AVCaptureDevicePositionUnspecified,
        ).devices()
    else:
        devices = AVFoundation.AVCaptureDevice.devicesWithMediaType_(AVFoundation.AVMediaTypeMuxed)
    return [d for d in devices if d.modelID() == 'iOS Device']


def find_device(name: str | None, timeout: float = 3.0):
    """Find the screen-capture device of the iPhone with the given name.

    The device appears a moment after enable_screen_capture_devices(), so this polls
    until `timeout`. Falls back to the only iOS device if none matches the name.
    """
    deadline = time.monotonic() + timeout
    while True:
        devices = _ios_capture_devices()
        match = next((d for d in devices if d.localizedName() == name), None)
        if match is None and len(devices) == 1:
            match = devices[0]
        if match is not None or time.monotonic() >= deadline:
            return match
        time.sleep(0.25)


class _FrameDelegate(NSObject):
    """AVCaptureVideoDataOutput delegate — converts each sample buffer to a QImage."""

    def initWithCallback_(self, callback):
        self = objc.super(_FrameDelegate, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def captureOutput_didOutputSampleBuffer_fromConnection_(self, output, sample_buffer, connection):
        # Exceptions must not propagate into AVFoundation's dispatch queue
        try:
            pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample_buffer)
            if pixel_buffer is None:
                return
            Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, Quartz.kCVPixelBufferLock_ReadOnly)
            try:
                width = Quartz.CVPixelBufferGetWidth(pixel_buffer)
                height = Quartz.CVPixelBufferGetHeight(pixel_buffer)
                bytes_per_row = Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer)
                pixels = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer).as_buffer(bytes_per_row * height)
                # Wrap the locked buffer without copying, then copy once into memory QImage owns
                image = QImage(pixels, width, height, bytes_per_row, QImage.Format.Format_RGB32).copy()
            finally:
                Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, Quartz.kCVPixelBufferLock_ReadOnly)
            self._callback(image)
        except Exception as e:
            logger.error(f"Video frame conversion failed: {e}")


class VideoStream:
    """Live screen stream of one iOS device. Calls on_frame(QImage) from a background queue."""

    def __init__(self, device, on_frame):
        self._session = AVFoundation.AVCaptureSession.alloc().init()

        device_input, error = AVFoundation.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
        if device_input is None or not self._session.canAddInput_(device_input):
            raise RuntimeError(f"Cannot open {device.localizedName()}: {error}")
        self._session.addInput_(device_input)

        output = AVFoundation.AVCaptureVideoDataOutput.alloc().init()
        # 32BGRA has the same memory layout as QImage.Format_RGB32, so no pixel conversion is needed
        output.setVideoSettings_({
            Quartz.kCVPixelBufferPixelFormatTypeKey: Quartz.kCVPixelFormatType_32BGRA,
        })
        output.setAlwaysDiscardsLateVideoFrames_(True)
        self._delegate = _FrameDelegate.alloc().initWithCallback_(on_frame)
        self._queue = libdispatch.dispatch_queue_create(b'iphone-mirror.video', None)
        output.setSampleBufferDelegate_queue_(self._delegate, self._queue)
        if not self._session.canAddOutput_(output):
            raise RuntimeError("Cannot add video output")
        self._session.addOutput_(output)

        # While streaming, iOS treats the Mac as an audio accessory and may route sound to it
        # (as with QuickTime) — play it there so it isn't lost
        audio_output = AVFoundation.AVCaptureAudioPreviewOutput.alloc().init()
        audio_output.setVolume_(1.0)
        if self._session.canAddOutput_(audio_output):
            self._session.addOutput_(audio_output)

    def start(self):
        self._session.startRunning()

    def stop(self):
        self._session.stopRunning()

    @property
    def is_running(self) -> bool:
        return bool(self._session.isRunning())
