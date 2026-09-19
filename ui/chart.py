"""自绘折线图控件（主窗口用）。悬浮窗的迷你走势是自己画的，不在这里。

统一约定：样本是 (timestamp, rtt|None) 的列表，None 表示丢包，
在图上画成红色的丢包柱。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QWidget

from . import theme

_GRID = QColor(232, 236, 244)
_AXIS_TEXT = QColor(152, 161, 176)


def nice_step(cap: float) -> float:
    """挑一个好看的 Y 轴刻度间隔。"""
    for step in (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000):
        if cap / step <= 4.2:
            return float(step)
    return 10000.0


def compute_cap(values: list[float | None], floor: float = 60.0) -> float:
    real = [v for v in values if v is not None]
    if not real:
        return floor
    top = max(real)
    step = nice_step(max(top * 1.25, floor))
    return max(floor, step * max(1, int(top * 1.25 / step) + 1))


class LineChart(QWidget):
    """主窗口的完整折线图：Y 轴刻度、丢包柱、平均线、鼠标悬停读数。"""

    PAD_L = 44
    PAD_R = 12
    PAD_T = 12
    PAD_B = 22

    def __init__(self, color: str = theme.ACCENT, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self._color = QColor(color)
        self._samples: list[tuple[float, float | None]] = []
        self._window = 300.0
        self._hover_idx: int | None = None

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def set_data(self, samples: list[tuple[float, float | None]], window: float) -> None:
        self._samples = samples
        self._window = max(10.0, window or 10.0)
        if self._hover_idx is not None and self._hover_idx >= len(samples):
            self._hover_idx = None
        self.update()

    # ------------------------------------------------------------ 坐标换算

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self.PAD_L,
            self.PAD_T,
            max(1.0, self.width() - self.PAD_L - self.PAD_R),
            max(1.0, self.height() - self.PAD_T - self.PAD_B),
        )

    def _bounds(self) -> tuple[float, float]:
        """时间轴范围：最后一个样本贴右边缘，往左一个 span。

        用样本时间而不是当前时钟做右边界，这样暂停时曲线会静止不动。
        `_window` 是调用方算好的"实际要显示多长"（数据不足整窗口时就等于实际跨度），
        所以刻度标签也直接用它 —— 标签必须和坐标位置一一对应，不能各说各话。
        """
        if not self._samples:
            return 0.0, 1.0
        t1 = self._samples[-1][0]
        return t1 - self._window, t1

    def _xof(self, ts: float) -> float:
        t0, t1 = self._bounds()
        r = self._plot_rect()
        return r.left() + (ts - t0) / max(1e-6, (t1 - t0)) * r.width()

    def _yof(self, v: float, cap: float) -> float:
        r = self._plot_rect()
        return r.bottom() - min(v, cap) / cap * r.height()

    # ------------------------------------------------------------ 事件

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._samples:
            return
        x = event.position().x()
        if x < self.PAD_L - 6 or x > self.width() - self.PAD_R + 6:
            self._hover_idx = None
            self.update()
            return
        best, best_d = None, 1e18
        for i, (ts, _v) in enumerate(self._samples):
            d = abs(self._xof(ts) - x)
            if d < best_d:
                best, best_d = i, d
        if best != self._hover_idx:
            self._hover_idx = best
            self.update()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        if self._hover_idx is not None:
            self._hover_idx = None
            self.update()

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self._plot_rect()
        samples = self._samples
        vals = [v for _t, v in samples]
        cap = compute_cap(vals, floor=60.0)
        step = nice_step(cap)

        # 背景
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(250, 251, 253)))
        p.drawRoundedRect(r.adjusted(-6, -6, 6, 6), 8, 8)

        if not samples:
            p.setPen(QPen(_AXIS_TEXT))
            f = QFont(p.font())
            f.setPointSizeF(9.5)
            p.setFont(f)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "正在采集数据…")
            return

        # 网格 + Y 轴标签
        f = QFont(p.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        fm = QFontMetricsF(f)
        y = step
        while y <= cap + 1e-6:
            yy = self._yof(y, cap)
            p.setPen(QPen(_GRID, 1))
            p.drawLine(QPointF(r.left(), yy), QPointF(r.right(), yy))
            label = f"{y:.0f}"
            p.setPen(QPen(_AXIS_TEXT))
            p.drawText(
                QRectF(0, yy - fm.height() / 2, self.PAD_L - 8, fm.height()),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            y += step

        p.setPen(QPen(_GRID, 1))
        p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.bottom()))

        # 丢包柱
        rtts = [v for v in vals if v is not None]
        avg = sum(rtts) / len(rtts) if rtts else None
        loss_col = QColor(230, 80, 60, 95)
        p.setBrush(QBrush(loss_col))
        p.setPen(Qt.PenStyle.NoPen)
        n = len(samples)
        col_w = max(1.5, min(6.0, r.width() / max(1, min(n, 400))))
        for ts, v in samples:
            if v is None:
                p.drawRect(QRectF(self._xof(ts) - col_w / 2, r.top(), col_w, r.height()))

        # 折线 + 区域渐变填充
        path = QPainterPath()
        fill = QPainterPath()
        started = False
        fill_started = False
        first_pt = last_pt = None
        for ts, v in samples:
            if v is None:
                started = False
                continue
            pt = QPointF(self._xof(ts), self._yof(v, cap))
            if started:
                path.lineTo(pt)
            else:
                path.moveTo(pt)
                if first_pt is None:
                    first_pt = pt
                started = True
            last_pt = pt
            # 填充路径必须显式 moveTo 起头，否则空路径上的 lineTo 会从 (0,0) 拉出一条斜线
            if fill_started:
                fill.lineTo(pt)
            else:
                fill.moveTo(pt)
                fill_started = True
        if first_pt and last_pt:
            fill.lineTo(last_pt.x(), r.bottom())
            fill.lineTo(first_pt.x(), r.bottom())
            fill.closeSubpath()
            grad = QLinearGradient(0, r.top(), 0, r.bottom())
            c1 = QColor(self._color)
            c1.setAlpha(52)
            c2 = QColor(self._color)
            c2.setAlpha(4)
            grad.setColorAt(0.0, c1)
            grad.setColorAt(1.0, c2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(grad))
            p.drawPath(fill)

        pen = QPen(self._color, 1.8)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        # 平均线
        if avg is not None:
            ay = self._yof(avg, cap)
            dash = QPen(QColor(120, 132, 150, 150), 1)
            dash.setStyle(Qt.PenStyle.DashLine)
            p.setPen(dash)
            p.drawLine(QPointF(r.left(), ay), QPointF(r.right(), ay))

        # X 轴时间标签：跨度就是 _window，和坐标换算用的是同一个值
        p.setPen(QPen(_AXIS_TEXT))
        t0, t1 = self._bounds()
        span = self._window
        for frac, text in (
            (0.0, f"{span:.0f}s 前"),
            (0.5, f"{span / 2:.0f}s 前"),
            (1.0, "现在"),
        ):
            xx = r.left() + frac * r.width()
            tw = fm.horizontalAdvance(text)
            if frac == 0.0:
                rect = QRectF(xx, r.bottom() + 4, tw + 6, fm.height())
            elif frac == 1.0:
                rect = QRectF(xx - tw - 4, r.bottom() + 4, tw + 6, fm.height())
            else:
                rect = QRectF(xx - tw / 2, r.bottom() + 4, tw, fm.height())
            p.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

        # 悬停读数
        idx = self._hover_idx
        if idx is not None and 0 <= idx < len(samples):
            ts, v = samples[idx]
            xx = self._xof(ts)
            p.setPen(QPen(QColor(140, 152, 170, 130), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(xx, r.top()), QPointF(xx, r.bottom()))
            if v is not None:
                yy = self._yof(v, cap)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(self._color))
                p.drawEllipse(QPointF(xx, yy), 3.2, 3.2)

            age = t1 - ts
            text = f"{age:.0f}s 前 · " + (f"{v:.0f} ms" if v is not None else "丢包")
            if v is None:
                text = f"{age:.0f}s 前 · 丢包"
            tw = fm.horizontalAdvance(text) + 12
            bx = min(max(xx - tw / 2, r.left()), r.right() - tw)
            box = QRectF(bx, r.top() + 4, tw, fm.height() + 7)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(30, 36, 48, 232)))
            p.drawRoundedRect(box, 4, 4)
            p.setPen(QPen(QColor(240, 244, 251)))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
