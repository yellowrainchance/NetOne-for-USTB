"""近 N 天用量柱状图（QPainter 手绘）。

刻意不用 QtCharts：那会把项目从 PySide6-Essentials 拖成需要 PySide6-Addons
（打包体积 +几十 MB），而且 QtCharts 的默认观感和这个浅色主题不搭。
手绘还能顺带做两件 QtCharts 做起来很啰嗦的事：超预算的日子直接染红、
日预算画一条虚线基准。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath
from PySide6.QtWidgets import QToolTip, QWidget

from . import theme
from .chart import nice_step


class BarChart(QWidget):
    """按天画柱子的用量图。

    set_data(days, budget_gb) 里 days 是 [(标签, 用量GB), ...]，按时间升序。
    """

    PAD_L = 38.0
    PAD_R = 8.0
    PAD_T = 10.0
    PAD_B = 18.0   # 至少 16：X 轴日期画在 plot.bottom()+2 起的 14px 里

    def __init__(self, parent=None):
        super().__init__(parent)
        self._days: list[tuple[str, float]] = []
        self._budget = 0.0
        self._hover = -1
        self.setMouseTracking(True)
        self.setMinimumHeight(112)

    # ------------------------------------------------------------ 数据
    def set_data(self, days: list[tuple[str, float]], budget_gb: float = 0.0) -> None:
        self._days = list(days or [])
        self._budget = float(budget_gb or 0.0)
        if self._hover >= len(self._days):
            self._hover = -1
        self.update()

    # ------------------------------------------------------------ 几何
    def _plot(self) -> QRectF:
        return QRectF(self.PAD_L, self.PAD_T,
                      max(10.0, self.width() - self.PAD_L - self.PAD_R),
                      max(10.0, self.height() - self.PAD_T - self.PAD_B))

    def _cap(self) -> float:
        peak = max([v for _l, v in self._days] + [self._budget, 0.0])
        if peak <= 0:
            return 1.0
        step = nice_step(peak * 1.15)
        return max(step, (int(peak * 1.15 / step) + 1) * step)

    def _bar_rect(self, idx: int) -> QRectF:
        plot = self._plot()
        n = max(1, len(self._days))
        slot = plot.width() / n
        bw = max(2.0, min(26.0, slot * 0.62))
        cx = plot.left() + (idx + 0.5) * slot
        cap = self._cap()
        value = self._days[idx][1]
        h = 0.0 if cap <= 0 else min(1.0, value / cap) * plot.height()
        return QRectF(cx - bw / 2, plot.bottom() - h, bw, h)

    # ------------------------------------------------------------ 交互
    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._days:
            return
        plot = self._plot()
        slot = plot.width() / max(1, len(self._days))
        idx = int((event.position().x() - plot.left()) // slot)
        idx = idx if 0 <= idx < len(self._days) else -1
        if idx != self._hover:
            self._hover = idx
            self.update()
        if idx >= 0:
            label, value = self._days[idx]
            QToolTip.showText(event.globalPosition().toPoint(),
                              f"{label}　用量 {value:.2f} GB", self)
        super().mouseMoveEvent(event)

    def leaveEvent(self, _event) -> None:  # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot()

        if not self._days:
            p.setPen(QColor(theme.TEXT_FAINT))
            p.setFont(theme.ui_font(11))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "还没有历史数据")
            p.end()
            return

        cap = self._cap()
        grid_pen_color = QColor(theme.BORDER_SOFT)
        label_font = theme.ui_font(9)
        p.setFont(label_font)
        fm = QFontMetricsF(label_font)

        # 横向网格 + Y 轴刻度
        step = nice_step(cap) if cap > 0 else 1.0
        ticks = []
        v = 0.0
        while v <= cap + 1e-6:
            ticks.append(v)
            v += step
        for value in ticks:
            y = plot.bottom() - (value / cap) * plot.height()
            p.setPen(grid_pen_color)
            p.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
            p.setPen(QColor(theme.TEXT_FAINT))
            p.drawText(QRectF(0, y - 7, self.PAD_L - 6, 14),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{value:g}")

        # 日预算虚线
        if self._budget > 0 and self._budget <= cap:
            y = plot.bottom() - (self._budget / cap) * plot.height()
            pen = p.pen()
            pen.setColor(QColor(theme.QUOTA_AMBER))
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))

        # 柱子
        over_color = QColor(theme.QUOTA_RED)
        normal_color = QColor(theme.ACCENT)
        hover_color = QColor(theme.ACCENT).darker(125)
        for i, (_label, value) in enumerate(self._days):
            rect = self._bar_rect(i)
            if rect.height() <= 0:
                continue
            if self._budget > 0 and value > self._budget:
                color = over_color
            elif i == self._hover:
                color = hover_color
            else:
                color = normal_color
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            radius = min(3.0, rect.width() / 2)
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            p.drawPath(path)

        # X 轴标签：只画几个等距点，免得挤成一团。
        # 末位（今天）必须补上：等距抽样在 n 不是 every 整数倍时会把它漏掉，
        # 轴的右端就停在前几天，看着像数据没在更新。
        n = len(self._days)
        every = max(1, n // 6)
        cols = list(range(0, n, every))
        if cols and cols[-1] != n - 1:
            cols.append(n - 1)
        half = 24.0
        p.setPen(QColor(theme.TEXT_FAINT))
        for i in cols:
            cx = self._bar_rect(i).center().x()
            # 水平夹在控件内：最后一根柱子贴着右边界，不夹的话日期会被裁掉半个
            left = min(max(cx - half, 0.0), max(0.0, self.width() - half * 2))
            p.drawText(QRectF(left, plot.bottom() + 2, half * 2, 14),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       self._days[i][0])
        p.end()
