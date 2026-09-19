"""程序图标：程序化绘制（不依赖图片资源文件），同时供托盘和 exe 打包使用。"""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

# 折线形状，按 0~1 的相对坐标给出
_STROKE = [(0.17, 0.63), (0.33, 0.40), (0.48, 0.56), (0.66, 0.25), (0.84, 0.45)]


def icon_pixmap(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#2f6fed")))
    p.drawRoundedRect(0, 0, size, size, size * 0.24, size * 0.24)

    pen = QPen(QColor(255, 255, 255), max(1.2, size * 0.078))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    path = QPainterPath()
    for i, (fx, fy) in enumerate(_STROKE):
        pt = QPointF(fx * size, fy * size)
        if i == 0:
            path.moveTo(pt)
        else:
            path.lineTo(pt)
    p.drawPath(path)
    p.end()
    return pm


def icon() -> QIcon:
    ic = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(icon_pixmap(size))
    return ic
