#!/usr/bin/env python3
"""
Draws the app icon (assets/AppIcon.png, 1024x1024): an iPhone on a blue macOS-style tile,
with a Mac pointer on its screen. build_app.sh turns it into AppIcon.icns.

    .venv/bin/python packaging/make_icon.py
"""

import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF
from PyQt6.QtGui import QGuiApplication

SIZE = 1024
OUTPUT = Path(__file__).resolve().parent.parent / 'assets' / 'AppIcon.png'


def rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def draw(painter: QPainter):
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Tile on Apple's icon grid: 824 pt body inside the 1024 canvas
    tile = QRectF(100, 100, 824, 824)
    shadow = QColor(0, 0, 0, 0)
    for i in range(12, 0, -1):  # soft drop shadow
        shadow.setAlpha(6)
        painter.fillPath(rounded(tile.translated(0, 8 + i).adjusted(-i, -i, i, i), 185 + i), shadow)
    gradient = QLinearGradient(tile.topLeft(), tile.bottomRight())
    gradient.setColorAt(0, QColor('#5b9dff'))
    gradient.setColorAt(1, QColor('#2340d6'))
    painter.fillPath(rounded(tile, 185), gradient)

    # iPhone
    phone = QRectF(512 - 165, 512 - 320 + 10, 330, 640)
    painter.fillPath(rounded(phone.translated(0, 14), 72), QColor(0, 0, 40, 70))
    painter.fillPath(rounded(phone, 72), QColor('#f5f7ff'))
    screen = phone.adjusted(20, 20, -20, -20)
    screen_gradient = QLinearGradient(screen.topLeft(), screen.bottomLeft())
    screen_gradient.setColorAt(0, QColor('#17214a'))
    screen_gradient.setColorAt(1, QColor('#0b1026'))
    painter.fillPath(rounded(screen, 54), screen_gradient)
    island = QRectF(screen.center().x() - 48, screen.top() + 22, 96, 28)
    painter.fillPath(rounded(island, 14), QColor('black'))

    # Home screen apps
    colors = ['#ff6b6b', '#ffd166', '#06d6a0', '#4cc9f0', '#b388ff', '#ff9f43']
    app, gap = 62, 22
    left = screen.center().x() - (3 * app + 2 * gap) / 2
    for i, color in enumerate(colors):
        row, col = divmod(i, 3)
        rect = QRectF(left + col * (app + gap), screen.top() + 92 + row * (app + gap), app, app)
        painter.fillPath(rounded(rect, 16), QColor(color))

    # Mac pointer, tip on the screen
    tip = QPointF(500, 560)
    scale = 1.55
    outline = [(0, 0), (0, 168), (40, 130), (68, 196), (98, 184), (70, 120), (122, 120)]
    pointer = QPolygonF([QPointF(tip.x() + x * scale, tip.y() + y * scale) for x, y in outline])
    painter.setPen(QPen(QColor(0, 0, 30, 90), 26, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin))
    painter.drawPolygon(pointer.translated(6, 12))
    painter.setPen(QPen(QColor('white'), 22, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(QColor('#111111'))
    painter.drawPolygon(pointer)


def main():
    app = QGuiApplication(sys.argv)  # noqa: F841 — fonts and painting need an application
    image = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    draw(painter)
    painter.end()
    OUTPUT.parent.mkdir(exist_ok=True)
    image.save(str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == '__main__':
    main()
