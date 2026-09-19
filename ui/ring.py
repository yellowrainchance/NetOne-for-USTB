"""环形进度控件与配色（托盘图标、悬浮窗、校园网页面共用）。

三处都要画同一个环（页面上的控件、悬浮窗自绘、托盘图标），所以核心绘制逻辑
抽成一个纯函数 `draw_ring()`，谁要画谁调，避免三份实现慢慢长歪。
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import theme

# 兼容旧名字：原来 NetQuota 的 ui/ring.py 里这几个常量是模块级的
GREEN = theme.QUOTA_GREEN
AMBER = theme.QUOTA_AMBER
RED = theme.QUOTA_RED
GRAY = theme.QUOTA_GRAY
TRACK = theme.RING_TRACK

pick_color = theme.quota_color


def draw_ring(p: QPainter, rect: QRectF, pct: float, color: str | QColor,
              thickness: float, track: str | QColor = TRACK,
              start_angle: float = 90.0) -> None:
    """在 rect 里画一个环形进度。

    pct 是"已完成比例"（0-100），从 12 点方向顺时针填充。
    drawArc 的角度单位是 1/16 度，所以乘 16；负值表示顺时针。
    """
    pen = QPen(QColor(track) if isinstance(track, str) else track, thickness)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(rect)

    pct = max(0.0, min(100.0, float(pct)))
    if pct > 0:
        pen.setColor(QColor(color) if isinstance(color, str) else color)
        p.setPen(pen)
        p.drawArc(rect, int(start_angle * 16), -int(pct * 360 * 16 / 100))


class RingProgress(QWidget):
    """QPainter 自绘环形进度控件。"""

    def __init__(self, parent=None, size: int = 72, thickness: int = 8,
                 track: str = TRACK, color: str = GREEN):
        super().__init__(parent)
        self._pct = 0.0
        self._size = size
        self._thickness = thickness
        self._track = track
        self._color = color
        self.setFixedSize(size, size)

    def set_percent(self, pct: float) -> None:
        pct = max(0.0, min(100.0, pct))
        if abs(pct - self._pct) < 1e-6:
            return
        self._pct = pct
        self.update()

    def percent(self) -> float:
        return self._pct

    def set_color(self, color: str) -> None:
        if color == self._color:
            return
        self._color = color
        self.update()

    def set_thickness(self, thickness: int) -> None:
        self._thickness = thickness
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        margin = self._thickness / 2 + 1
        rect = QRectF(margin, margin,
                      self._size - 2 * margin, self._size - 2 * margin)
        draw_ring(p, rect, self._pct, self._color, self._thickness, self._track)
        p.end()


def make_tray_icon(pct_left: float, online: bool = True, size: int = 64) -> "QIcon":
    """生成托盘用的环形图标（离线时画一条横杠）。"""
    from PySide6.QtGui import QIcon, QPixmap

    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    thick = max(4, size // 10)
    margin = thick / 2 + 1
    rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)

    if online:
        draw_ring(p, rect, max(0.0, min(100.0, pct_left)), pick_color(pct_left), thick)
    else:
        pen = QPen(QColor(TRACK), thick)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawEllipse(rect)
        pen = QPen(QColor(GRAY), max(2.0, thick * 0.6))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(int(margin), size // 2, size - int(margin), size // 2)
    p.end()
    return QIcon(pm)
