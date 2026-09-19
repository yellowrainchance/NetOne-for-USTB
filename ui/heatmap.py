"""当月每日用量热力图（GitHub 风格单行版）。"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QToolTip, QWidget

C0 = "#E8E9EC"      # 0
C1 = "#9BE9A8"      # <=0.5x 日预算
C2 = "#40C463"      # <=1.0x
C3 = "#216E39"      # <=1.5x
C4 = "#E24B4A"      # >1.5x（显著超预算，红色警示）


def _color(ratio: float) -> str:
    if ratio <= 0:
        return C0
    if ratio <= 0.5:
        return C1
    if ratio <= 1.0:
        return C2
    if ratio <= 1.5:
        return C3
    return C4


class MonthHeatmap(QWidget):
    """一行 N 个方块，一个方块 = 当月某一天的计费用量。

    usage:  set_data(used_by_day: dict[int, float], budget_gb: float)
            键为 1..今天 的日号，值为当天 GB 用量。
    """

    CELL = 12
    GAP = 3
    MAX_DAYS = 31

    def __init__(self, parent=None):
        super().__init__(parent)
        self._values: dict[int, float] = {}
        self._budget = 0.0
        self._days_in_month = 31
        self._today_day = 1
        self._month = date.today().strftime("%Y-%m")
        self.setMouseTracking(True)
        w = self.MAX_DAYS * (self.CELL + self.GAP) + self.GAP
        self.setFixedHeight(self.CELL + 6)
        self.setMinimumWidth(w)
        self.setToolTip("鼠标悬停查看每日用量；色阶按「日预算」分级")

    # ------------------------------------------------------------ 数据
    def set_data(self, used_by_day: dict[int, float], budget_gb: float,
                 month: str | None = None, days_in_month: int = 31,
                 today_day: int = 1) -> None:
        self._values = used_by_day
        self._budget = budget_gb
        if month:
            self._month = month
        self._days_in_month = days_in_month
        self._today_day = today_day
        self.update()

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        x0 = self.GAP
        for day in range(1, self._days_in_month + 1):
            x = x0 + (day - 1) * (self.CELL + self.GAP)
            used = self._values.get(day, 0.0)
            ratio = used / self._budget if self._budget > 0 else used
            color = _color(ratio)
            rect = QRect(x, 3, self.CELL, self.CELL)
            if day > self._today_day:
                # 未来日期：细线框占位
                p.setPen(QColor("#C9CDD2"))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(rect)
                continue
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(color))
            p.drawRoundedRect(rect, 3, 3)
        p.end()

    # ------------------------------------------------------------ 交互
    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        col = (pos.x() - self.GAP) // (self.CELL + self.GAP)
        if 0 <= col < self._days_in_month:
            day = col + 1
            if day <= self._today_day:
                used = self._values.get(day, 0.0)
                budget = self._budget or 0.0
                tip = f"{self._month}-{day:02d}\n用量 {used:.2f} GB"
                if budget > 0:
                    tip += f"\n日预算 {budget:.2f} GB（{used / budget:.0%}）"
                QToolTip.showText(event.globalPosition().toPoint(), tip, self)
        super().mouseMoveEvent(event)
