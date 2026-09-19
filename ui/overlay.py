"""桌面悬浮窗：置顶、半透明、可拖动。

合并后它同时承担两件事：
  * 顶部一块额度区（环形进度 + 剩余 GB 大字）—— 校园网额度
  * 下面若干行延迟（各目标实时 RTT + 迷你曲线）—— 延迟监测

整个窗口自绘（不依赖子控件），这样在游戏画面上更轻、更好控制样式，
也能自由调整紧凑/详细两种密度的排版。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QMenu, QWidget

from . import theme
from .chart import compute_cap
from .ring import draw_ring
from core.config import OVERLAY_TITLE

ROW_H = 40
ROW_H_COMPACT = 26
PAD = 6
QUOTA_H = 52            # 额度区高度（常规）
QUOTA_H_COMPACT = 38
QUOTA_H_NOSUB = 40      # 副行（已用/余额）全关时额度区收窄
QUOTA_H_COMPACT_NOSUB = 30
QUOTA_W_MIN = 250       # 额度区需要的宽度（环 + 大字 + 副标题）
RESIZE_MARGIN = 12      # 左右边缘/下角的热区宽度（px）


class OverlayWindow(QWidget):
    openMainRequested = Signal()
    focusTargetRequested = Signal(str)
    openQuotaRequested = Signal()
    quitRequested = Signal()
    # 位置/宽度落盘后发一次：设置对话框开着时要把宽度同步进它的输入框，
    # 不然对话框里的旧值会在下一次改动时把刚拖好的宽度写回去。
    geometryChanged = Signal()

    def __init__(self, config):
        super().__init__(None)
        self.cfg = config
        oc = config.overlay_cfg

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setWindowTitle(OVERLAY_TITLE)

        self._rows: list[dict] = []
        self._quota: dict | None = None
        self._width = int(oc.get("w") or 300)
        self._compact = bool(oc.get("compact"))
        self._locked = bool(oc.get("locked"))
        self._opacity = float(oc.get("opacity") or 0.86)
        # 内容逐项开关：额度环 / 已用流量 / 余额 / 单行当前延迟 / 各目标延迟行。
        # 全是"用户看得见才叫生效"的项 —— 改任何一个，高度和绘制都要跟着变。
        self._show_quota = bool(oc.get("show_quota", True))
        self._show_used = bool(oc.get("show_used", True))
        self._show_balance = bool(oc.get("show_balance", True))
        self._show_ping = bool(oc.get("show_ping", True))
        self._show_latency = bool(oc.get("show_latency", True))
        # 「当前延迟」单行取哪个目标：空 = 第一个启用的目标
        self._ping_tid = str(oc.get("ping_tid") or "")

        self._mode = ""
        self._press_global = QPoint()
        self._start_pos = QPoint()
        self._start_w = 0
        self._hover_row = -1
        self._warn = False
        self._hover_quota = False

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(700)
        self._save_timer.timeout.connect(self._persist)

        self._restore_geometry()

    # ------------------------------------------------------------ 几何

    def _restore_geometry(self) -> None:
        oc = self.cfg.overlay_cfg
        self._apply_size()
        x, y = oc.get("x"), oc.get("y")
        if x is None or y is None:
            screen = QGuiApplication.primaryScreen()
            area = screen.availableGeometry() if screen else None
            if area:
                x = area.right() - self.width() - 24
                y = area.top() + 24
            else:
                x, y = 40, 40
        self.move(int(x), int(y))

    def _row_h(self) -> int:
        return ROW_H_COMPACT if self._compact else ROW_H

    def _quota_h(self) -> int:
        if self._compact:
            return QUOTA_H_COMPACT if self._has_sub() else QUOTA_H_COMPACT_NOSUB
        return QUOTA_H if self._has_sub() else QUOTA_H_NOSUB

    def _has_sub(self) -> bool:
        """额度区副行（已用流量 / 余额）至少开一项才画。"""
        return self._show_used or self._show_balance

    def _ping_visible(self) -> bool:
        """「当前延迟」单行：开着且引擎确实吐过数据（engine 停时 app 会喂空行）。"""
        return self._show_ping and bool(self._rows)

    def _ping_row(self) -> dict | None:
        """「当前延迟」显示的目标行：按 ping_tid 选，选不上（已删/已停用）退回第一个。"""
        if not self._rows:
            return None
        if self._ping_tid:
            for row in self._rows:
                if row.get("tid") == self._ping_tid:
                    return row
        return self._rows[0]

    def _visible_rows(self) -> int:
        if not self._show_latency:
            return 0
        return len(self._rows)

    def _apply_size(self) -> None:
        """按"当前显示哪几块"算高度。

        额度区 / 当前延迟行 / 各目标延迟行都可以单独关掉，高度必须跟着算；
        相邻块之间加 1px 分隔线。全关时保底一行高，别变成 0。
        """
        blocks: list[float] = []
        if self._show_quota:
            blocks.append(self._quota_h())
        if self._ping_visible():
            blocks.append(self._row_h())
        rows = self._visible_rows()
        if rows:
            blocks.append(rows * self._row_h())
        if not blocks:
            blocks.append(self._row_h())      # 全关也别变成 0 高
        seps = len(blocks) - 1                # 每两个相邻块之间 1px 分隔线
        h = PAD * 2 + sum(blocks) + seps
        self.setFixedSize(self._width, int(round(h)))

    def min_width(self) -> int:
        return 200

    def max_width(self) -> int:
        return 560

    def apply_settings(self) -> None:
        """把配置里的悬浮窗设置重新读进来并应用。

        设置对话框里改的外观要立刻看得见 —— 否则用户得去悬浮窗上右键再调一遍，
        而且两边会显示成不一致。
        """
        oc = self.cfg.overlay_cfg
        self._width = max(self.min_width(), min(self.max_width(), int(oc.get("w") or 300)))
        self._compact = bool(oc.get("compact"))
        self._locked = bool(oc.get("locked"))
        self._opacity = max(0.35, min(1.0, float(oc.get("opacity") or 0.86)))
        self._show_quota = bool(oc.get("show_quota", True))
        self._show_used = bool(oc.get("show_used", True))
        self._show_balance = bool(oc.get("show_balance", True))
        self._show_ping = bool(oc.get("show_ping", True))
        self._show_latency = bool(oc.get("show_latency", True))
        self._ping_tid = str(oc.get("ping_tid") or "")
        self._apply_size()
        self.update()

    # ------------------------------------------------------------ 数据

    def set_rows(self, rows: list[dict]) -> None:
        """rows: {tid, name, color, rtt, loss, jitter, grade, status, values}"""
        self._rows = rows
        self._warn = any(r.get("grade") in ("bad", "poor") for r in rows)
        self._apply_size()
        self.update()

    def set_quota(self, info: dict | None) -> None:
        """info: {left_gb, pct_left, used_gb, total_gb, online, source, error}"""
        self._quota = info
        self.update()

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)

        bg = QColor(theme.OV_BG)
        bg.setAlphaF(max(0.35, min(1.0, self._opacity)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(rect, 9, 9)

        edge = QColor(theme.OV_LOSS if self._warn else theme.OV_BORDER)
        edge.setAlphaF(0.95 if self._warn else 0.8)
        p.setPen(QPen(edge, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 9, 9)

        y = PAD
        sep_color = QColor(255, 255, 255, 22)
        if self._show_quota:
            self._paint_quota(p, y)
            y += self._quota_h()

        if self._ping_visible():
            if y > PAD:
                p.setPen(QPen(sep_color, 1))
                p.drawLine(10, int(y), self.width() - 10, int(y))
                y += 1
            self._paint_ping(p, y)
            y += self._row_h()

        rows = self._visible_rows()
        if not rows:
            if not self._show_quota and not self._ping_visible():
                p.setPen(QPen(QColor(theme.OV_DIM)))
                p.setFont(self._font(11))
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "无监测内容")
            return

        if y > PAD:
            p.setPen(QPen(sep_color, 1))
            p.drawLine(10, int(y), self.width() - 10, int(y))
            y += 1

        row_h = self._row_h()
        for i, row in enumerate(self._rows[:rows]):
            top = y + i * row_h
            self._paint_row(p, row, top, row_h, hovered=(i == self._hover_row))
            if i < rows - 1:
                p.setPen(QPen(sep_color, 1))
                p.drawLine(10, int(top + row_h), self.width() - 10, int(top + row_h))

    # ------------------------------------------------------------ 额度区

    def _paint_quota(self, p: QPainter, y: int) -> None:
        h = self._quota_h()
        info = self._quota or {}
        online = bool(info.get("online"))
        total = float(info.get("total_gb") or 0.0)
        left = float(info.get("left_gb") or 0.0)
        used = float(info.get("used_gb") or 0.0)
        pct_left = float(info.get("pct_left") or 0.0)
        color = QColor(theme.quota_color(pct_left, online))

        if self._hover_quota:
            hl = QColor(255, 255, 255, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(hl))
            p.drawRoundedRect(QRectF(4, y + 2, self.width() - 8, h - 4), 6, 6)

        # 环形
        ring = float(h - 16)
        ring = max(26.0, min(40.0, ring))
        ring_rect = QRectF(12, y + (h - ring) / 2.0, ring, ring)
        if online:
            draw_ring(p, ring_rect, pct_left, color, max(3.0, ring / 8.0))
        else:
            draw_ring(p, ring_rect, 0.0, theme.OV_DIM, max(3.0, ring / 8.0))

        # 大字：剩余额度
        big_font = self._font(15 if not self._compact else 12, True)
        p.setFont(big_font)
        p.setPen(QPen(QColor(theme.OV_TEXT)))
        text_left = ring_rect.right() + 10
        text_w = self.width() - text_left - 12

        if not info:
            main = "额度 --"
            sub = "尚未采集" if self._has_sub() else ""
        elif not online:
            main = "离线"
            sub = (info.get("error") or "未连接校园网")[:40] if self._has_sub() else ""
        elif left < 0:
            main = f"超额 {-left:.1f} GB"
            sub = self._quota_sub(used, total, None)
        else:
            main = f"剩余 {left:.1f} GB"
            sub = self._quota_sub(used, total, pct_left)
        if not self._has_sub():
            sub = ""

        fm = QFontMetricsF(big_font)
        main = fm.elidedText(main, Qt.TextElideMode.ElideRight, text_w)
        p.drawText(QRectF(text_left, y + 4, text_w, 20),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, main)

        sub_font = self._font(8.5)
        p.setFont(sub_font)
        p.setPen(QPen(QColor(theme.OV_DIM)))
        sfm = QFontMetricsF(sub_font)
        sub = sfm.elidedText(sub, Qt.TextElideMode.ElideRight, text_w)
        p.drawText(QRectF(text_left, y + h - 20, text_w, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sub)

    def _quota_sub(self, used: float, total: float, pct_left: float | None) -> str:
        """副行内容按开关拼装：已用流量 / 余额，至少开一项才会被调用。"""
        parts: list[str] = []
        if self._show_used:
            # 不带百分比前缀：环已经表达了剩余百分比，副行要给余额留地方，
            # 两项都开时整行超宽会被省略号吃掉（实测 300px 宽就截没了）
            parts.append(f"已用 {used:.1f} / {total:.0f} GB")
        bal = (self._quota or {}).get("balance_yuan")
        if self._show_balance and bal is not None:
            parts.append(f"余额 ¥{bal:.2f}")
        if not parts:
            parts.append(f"按 ¥{(self._quota or {}).get('price', 0):.2f}/GB 计费")
        return " · ".join(parts)

    def _quota_rect(self) -> QRectF:
        return QRectF(0, 0, self.width(), self._quota_h() + PAD)

    # ------------------------------------------------------------ 延迟行

    def _font(self, size: float, bold: bool = False) -> QFont:
        return theme.ui_font(size, bold)

    def _rows_top(self) -> float:
        y = float(PAD)
        if self._show_quota:
            y += self._quota_h()
            if self._ping_visible() or self._visible_rows():
                y += 1
        if self._ping_visible():
            y += self._row_h()
            if self._visible_rows():
                y += 1
        return y

    def _paint_ping(self, p: QPainter, y: int) -> None:
        """「当前延迟」单行：显示用户选定的目标（默认第一个启用目标）。

        主开关关闭 / 暂停时 app 会喂空行，这个块整体消失 —— 挂一个不再
        更新的旧数字比什么都不显示更误导。
        """
        row = self._ping_row()
        if row is None:
            return
        row_h = self._row_h()
        cy = y + row_h / 2.0
        grade = row.get("grade") or "unknown"
        state_color = QColor(theme.GRADE_COLORS.get(grade, theme.GRADE_COLORS["unknown"]))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(state_color))
        cx = 11 if self._compact else 13
        p.drawEllipse(QRectF(cx - 3.4, cy - 3.4, 6.8, 6.8))

        name_font = self._font(10 if self._compact else 11, True)
        p.setFont(name_font)
        fm = QFontMetricsF(name_font)
        rtt_text, rtt_color = self._rtt_text(row)
        rtt_font = self._font(11.5 if self._compact else 14, True)
        rtt_fm = QFontMetricsF(rtt_font)
        rtt_w = rtt_fm.horizontalAdvance(rtt_text) + 12
        avail = max(30.0, self.width() - (cx + 9) - rtt_w - 10)
        label = fm.elidedText(f"当前延迟 · {row.get('name', '')}",
                              Qt.TextElideMode.ElideRight, avail)
        p.setPen(QPen(QColor(theme.OV_TEXT)))
        p.drawText(QRectF(cx + 9, y, avail, row_h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)

        p.setFont(rtt_font)
        p.setPen(QPen(QColor(rtt_color)))
        p.drawText(QRectF(self.width() - rtt_w - 10, y, rtt_w, row_h),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, rtt_text)

    def _row_at(self, pos) -> int:
        if not self._show_latency:
            return -1
        top = self._rows_top()
        row_h = self._row_h()
        n = self._visible_rows()
        if top <= pos.y() < top + n * row_h:
            return int((pos.y() - top) // row_h)
        return -1

    def _paint_row(self, p: QPainter, row: dict, y: int, row_h: int, hovered: bool) -> None:
        w = self.width()
        cy = y + row_h / 2.0
        grade = row.get("grade") or "unknown"
        state_color = QColor(theme.GRADE_COLORS.get(grade, theme.GRADE_COLORS["unknown"]))

        if hovered and not self._compact:
            hl = QColor(255, 255, 255, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(hl))
            p.drawRoundedRect(QRectF(4, y + 2, w - 8, row_h - 4), 6, 6)

        # 状态点
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(state_color))
        cx = 13 if not self._compact else 11
        p.drawEllipse(QRectF(cx - 3.4, cy - 3.4, 6.8, 6.8))

        if self._compact:
            self._paint_compact_row(p, row, y, row_h, cx)
        else:
            self._paint_full_row(p, row, y, row_h, cx)

    def _paint_compact_row(self, p: QPainter, row: dict, y: int, row_h: int, cx: int) -> None:
        w = self.width()
        name_font = self._font(10.5, True)
        p.setFont(name_font)
        fm = QFontMetricsF(name_font)
        rtt_text, rtt_color = self._rtt_text(row)
        rtt_font = self._font(12, True)
        rtt_fm = QFontMetricsF(rtt_font)
        rtt_w = max(rtt_fm.horizontalAdvance(t) for t in ("999ms", "1.5s", "超时")) + 10

        name_x = cx + 9
        avail = max(20.0, w - name_x - rtt_w - 12)
        name = fm.elidedText(row.get("name", ""), Qt.TextElideMode.ElideRight, avail)
        p.setPen(QPen(QColor(theme.OV_TEXT)))
        p.drawText(QRectF(name_x, y, avail, row_h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
        p.setFont(rtt_font)
        p.setPen(QPen(QColor(rtt_color)))
        p.drawText(QRectF(w - rtt_w - 10, y, rtt_w, row_h),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, rtt_text)

    def _paint_full_row(self, p: QPainter, row: dict, y: int, row_h: int, cx: int) -> None:
        w = self.width()
        rtt_text, rtt_color = self._rtt_text(row)
        rtt_font = self._font(14, True)
        rtt_fm = QFontMetricsF(rtt_font)
        # 按最长可能出现的读数预留宽度，否则 "155ms" 会被裁成 "55ms"
        rtt_w = max(rtt_fm.horizontalAdvance(t) for t in ("999ms", "1.5s", "超时", "失败")) + 12

        spark_w = 56.0
        right = w - 10.0
        rtt_left = right - rtt_w
        spark_right = rtt_left - 8.0
        spark_left = spark_right - spark_w
        text_left = cx + 9.0
        text_w = max(40.0, spark_left - 8.0 - text_left)

        vals = row.get("values") or []
        if vals and spark_w > 12:
            self._paint_spark(
                p, vals, QRectF(spark_left, y + 10, spark_w, row_h - 20),
                QColor(row.get("color", theme.ACCENT)),
            )

        p.setFont(rtt_font)
        p.setPen(QPen(QColor(rtt_color)))
        p.drawText(QRectF(rtt_left, y, rtt_w, row_h),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, rtt_text)

        name_font = self._font(11, True)
        p.setFont(name_font)
        fm = QFontMetricsF(name_font)
        name = fm.elidedText(row.get("name", ""), Qt.TextElideMode.ElideRight, text_w)
        p.setPen(QPen(QColor(theme.OV_TEXT)))
        p.drawText(QRectF(text_left, y + 3, text_w, 15),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        sub_font = self._font(8.5)
        p.setFont(sub_font)
        sfm = QFontMetricsF(sub_font)
        loss = row.get("loss") or 0.0
        jitter = row.get("jitter")
        parts = [f"丢包{loss:.1f}%"]
        if jitter is not None:
            parts.append(f"抖动{jitter:.0f}ms")
        status = row.get("status")
        if status not in (None, "", "ok"):
            parts.append(theme.STATUS_LABEL.get(status, status))
        sub = sfm.elidedText(" · ".join(parts), Qt.TextElideMode.ElideRight, text_w)
        sub_color = QColor(theme.OV_LOSS) if loss >= 1.0 else QColor(theme.OV_DIM)
        p.setPen(QPen(sub_color))
        p.drawText(QRectF(text_left, y + row_h - 18, text_w, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sub)

    def _paint_spark(self, p: QPainter, vals: list, rect: QRectF, color: QColor) -> None:
        n = len(vals)
        if n < 2 or rect.width() < 6:
            return
        cap = compute_cap(vals, floor=50.0)
        step_x = rect.width() / n
        loss_col = QColor(230, 80, 60, 130)

        def xof(i: int) -> float:
            return rect.left() + (i + 0.5) * step_x

        def yof(v: float) -> float:
            return rect.bottom() - min(v, cap) / cap * rect.height()

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(loss_col))
        for i, v in enumerate(vals):
            if v is None:
                p.drawRect(QRectF(xof(i) - step_x * 0.3, rect.top(),
                                  max(1.0, step_x * 0.6), rect.height()))

        path = QPainterPath()
        started = False
        for i, v in enumerate(vals):
            if v is None:
                started = False
                continue
            point = QPointF(xof(i), yof(v))
            if started:
                path.lineTo(point)
            else:
                path.moveTo(point)
                started = True
        pen = QPen(color, 1.4)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

    @staticmethod
    def _rtt_text(row: dict) -> tuple[str, str]:
        rtt = row.get("rtt")
        grade = row.get("grade") or "unknown"
        color = theme.GRADE_COLORS.get(grade, theme.GRADE_COLORS["unknown"])
        if rtt is None:
            status = row.get("status")
            if status in ("timeout", None, ""):
                return "超时", theme.OV_LOSS
            return theme.STATUS_LABEL.get(status, "失败"), theme.OV_LOSS
        if rtt < 1.0:
            return "<1ms", color
        if rtt >= 1000:
            return f"{rtt / 1000:.1f}s", color
        return f"{rtt:.0f}ms", color

    # ------------------------------------------------------------ 交互

    def _zone_at(self, pos) -> str:
        """鼠标落在哪个调宽热区：左右边缘整条 + 两个下角，各 12px。"""
        m = RESIZE_MARGIN
        left = pos.x() <= m
        right = pos.x() >= self.width() - m
        bottom = pos.y() >= self.height() - m
        if right and bottom:
            return "resize-br"
        if left and bottom:
            return "resize-bl"
        if right:
            return "resize-r"
        if left:
            return "resize-l"
        return ""

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        self._press_global = event.globalPosition().toPoint()
        if self._locked:
            self._mode = ""
            return
        zone = self._zone_at(pos)
        if zone:
            self._mode = zone
            self._start_w = self.width()
            self._start_pos = self.pos()
        else:
            self._mode = "move"
            self._start_pos = self.pos()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if self._mode == "move":
            delta = event.globalPosition().toPoint() - self._press_global
            self.move(self._start_pos + delta)
        elif self._mode.startswith("resize"):
            delta = event.globalPosition().toPoint() - self._press_global
            # 右侧/右下角：往右拖变宽；左侧/左下角：往左拖变宽，
            # 同时把窗口往左推，视觉上才是"捏着左边缘"在拉。
            w = (self._start_w + delta.x() if self._mode in ("resize-r", "resize-br")
                 else self._start_w - delta.x())
            w = max(self.min_width(), min(self.max_width(), w))
            if w != self._width:
                if self._mode in ("resize-l", "resize-bl"):
                    self.move(self._start_pos.x() + (self._start_w - w),
                              self._start_pos.y())
                self._width = w
                self._apply_size()
        else:
            idx = self._row_at(pos)
            hover_quota = self._show_quota and self._quota_rect().contains(pos)
            if idx != self._hover_row or hover_quota != self._hover_quota:
                self._hover_row = idx
                self._hover_quota = hover_quota
                self.update()
            zone = "" if self._locked else self._zone_at(pos)
            if zone in ("resize-l", "resize-r"):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif zone == "resize-br":
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif zone == "resize-bl":
                self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            elif self._locked:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            else:
                self.setCursor(Qt.CursorShape.SizeAllCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._mode:      # move / resize-* 都要存
            self._save_timer.start()
        self._mode = ""

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self._show_quota and self._quota_rect().contains(event.position()):
            self.openQuotaRequested.emit()
            return
        idx = self._hover_row
        if 0 <= idx < len(self._rows) and self._rows[idx].get("tid"):
            self.focusTargetRequested.emit(self._rows[idx]["tid"])
        else:
            self.openMainRequested.emit()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        if self._hover_row != -1 or self._hover_quota:
            self._hover_row = -1
            self._hover_quota = False
            self.update()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        act_main = menu.addAction("打开主面板")
        act_quota = menu.addAction("看校园网额度")
        menu.addSeparator()
        act_show_quota = menu.addAction("显示额度区")
        act_show_quota.setCheckable(True)
        act_show_quota.setChecked(self._show_quota)
        act_show_used = menu.addAction("显示已用流量")
        act_show_used.setCheckable(True)
        act_show_used.setChecked(self._show_used)
        act_show_balance = menu.addAction("显示余额")
        act_show_balance.setCheckable(True)
        act_show_balance.setChecked(self._show_balance)
        act_show_ping = menu.addAction("显示当前延迟")
        act_show_ping.setCheckable(True)
        act_show_ping.setChecked(self._show_ping)
        act_show_latency = menu.addAction("显示各目标延迟行")
        act_show_latency.setCheckable(True)
        act_show_latency.setChecked(self._show_latency)
        act_compact = menu.addAction("紧凑模式")
        act_compact.setCheckable(True)
        act_compact.setChecked(self._compact)
        act_lock = menu.addAction("锁定位置")
        act_lock.setCheckable(True)
        act_lock.setChecked(self._locked)
        act_quit = menu.addAction("退出 NetOne")

        menu.addSeparator()
        # 「当前延迟」显示哪个目标（只有一个目标时子菜单也只有默认项，无碍）
        ping_menu = menu.addMenu("当前延迟来源")
        act_ping_auto = ping_menu.addAction("第一个目标")
        act_ping_auto.setCheckable(True)
        act_ping_auto.setChecked(not self._ping_tid)
        act_ping_auto.setData("")
        ping_actions = [act_ping_auto]
        for row in self._rows:
            act = ping_menu.addAction(row.get("name") or "未命名")
            act.setCheckable(True)
            act.setChecked(self._ping_tid == row.get("tid"))
            act.setData(str(row.get("tid") or ""))
            ping_actions.append(act)

        opacity_menu = menu.addMenu("背景不透明度")
        for label, val in (("40%", 0.40), ("60%", 0.60), ("75%", 0.75),
                           ("86%", 0.86), ("95%", 0.95)):
            act = opacity_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(abs(self._opacity - val) < 0.01)
            act.setData(val)

        width_menu = menu.addMenu("宽度")
        width_actions = []
        for label, val in (("窄", 240), ("标准", 300), ("宽", 360), ("加宽", 420)):
            act = width_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self._width == val)
            act.setData(val)
            width_actions.append(act)
        # 锁定的是位置和大小：锁定后右键也不能再改宽度，否则"锁定"名不副实
        width_menu.setEnabled(not self._locked)

        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen is act_main:
            self.openMainRequested.emit()
        elif chosen is act_quota:
            self.openQuotaRequested.emit()
        elif chosen is act_show_quota:
            self._show_quota = act_show_quota.isChecked()
            self._apply_size()
            self.update()
            self._save_timer.start()
        elif chosen is act_show_used:
            self._show_used = act_show_used.isChecked()
            self._apply_size()
            self.update()
            self._save_timer.start()
        elif chosen is act_show_balance:
            self._show_balance = act_show_balance.isChecked()
            self._apply_size()
            self.update()
            self._save_timer.start()
        elif chosen is act_show_ping:
            self._show_ping = act_show_ping.isChecked()
            self._apply_size()
            self.update()
            self._save_timer.start()
        elif chosen is act_show_latency:
            self._show_latency = act_show_latency.isChecked()
            self._apply_size()
            self.update()
            self._save_timer.start()
        elif chosen is act_compact:
            self._compact = act_compact.isChecked()
            self._apply_size()
            self.update()
            self._save_timer.start()
        elif chosen is act_lock:
            self._locked = act_lock.isChecked()
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._save_timer.start()
        elif chosen is act_quit:
            self.quitRequested.emit()
        elif chosen in width_actions:
            self._width = int(chosen.data())
            self._apply_size()
            self._save_timer.start()
        elif chosen in ping_actions:
            self._ping_tid = str(chosen.data())
            self.update()
            self._save_timer.start()
        elif chosen.parent() is opacity_menu:
            self._opacity = float(chosen.data())
            self.update()
            self._save_timer.start()

    def _persist(self) -> None:
        oc = self.cfg.overlay_cfg
        oc.update({
            "x": self.x(),
            "y": self.y(),
            "w": self._width,
            "compact": self._compact,
            "locked": self._locked,
            "opacity": self._opacity,
            "visible": self.isVisible(),
            "show_quota": self._show_quota,
            "show_used": self._show_used,
            "show_balance": self._show_balance,
            "show_ping": self._show_ping,
            "show_latency": self._show_latency,
            "ping_tid": self._ping_tid,
        })
        self.cfg.save()
        self.geometryChanged.emit()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._persist()
        super().closeEvent(event)
