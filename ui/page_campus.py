"""校园网额度页：剩余额度环 + 用量统计 + 设备入口。

从 NetQuota 第一代的主面板搬过来，主要变化：
  * 环和大字改用**接口 v4 口径**（第一代用的是门户 flow，偏小）；
  * 明确标出数据来源（接口口径 / 门户口径），并显示与门户 flow 的差值；
  * 原来的 QtCharts 柱状图换成自绘的 `ui/bars.py`，不再依赖 PySide6-Addons；
  * 设备管理单独开窗口，这里只留一个入口 + 在线台数。
"""
from __future__ import annotations

import calendar
from datetime import date

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.campus.models import Quota, Snapshot, SOURCE_API

from . import theme
from .bars import BarChart
from .heatmap import MonthHeatmap
from .ring import RingProgress
from .widgets import Badge, elide

# 口径徽章颜色
SOURCE_COLORS = {
    SOURCE_API: theme.GRADE_COLORS["good"],
    "portal": theme.GRADE_COLORS["fair"],
}


class _MetricCard(QFrame):
    def __init__(self, label: str, value: str = "--"):
        super().__init__()
        self.setObjectName("Card")
        self.setFrameShape(QFrame.Shape.NoFrame)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(11, 8, 11, 8)
        lay.setSpacing(2)
        self._label = QLabel(label)
        self._label.setObjectName("MetricLabel")
        self._value = QLabel(value)
        self._value.setObjectName("MetricValue")
        lay.addWidget(self._label)
        lay.addWidget(self._value)
        self._last: tuple[str, str | None] | None = None

    def set_label(self, text: str) -> None:
        if self._label.text() != text:
            self._label.setText(text)

    def set_value(self, text: str, color: str | None = None) -> None:
        if self._last == (text, color):
            return
        self._last = (text, color)
        self._value.setText(text)
        self._value.setStyleSheet(f"color: {color};" if color else "")


def _styled_bar() -> QProgressBar:
    bar = QProgressBar()
    bar.setTextVisible(False)
    bar.setFixedHeight(7)
    bar.setRange(0, 1000)
    return bar


def _tint(bar: QProgressBar, color: str) -> None:
    bar.setStyleSheet(
        f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}"
        f"QProgressBar {{ background: {theme.RING_TRACK}; border-radius: 3px; }}"
    )


class CampusPage(QWidget):
    """校园网额度页。"""

    refreshRequested = Signal()
    openDevicesRequested = Signal()
    statusChanged = Signal(str)

    def __init__(self, config, stats=None, store=None, parent=None):
        super().__init__(parent)
        self.cfg = config
        self.stats = stats
        self.store = store
        self.quota: Quota = config.quota()
        self._online_devices = 0
        self._build_ui()

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        # 9 而不是 11：这一页控件多（工具条/环/两条进度/柱状图/热力图/六张卡片），
        # 间距 11 时整页最小高度会顶到窗口最小高度以上，反而把布局挤到重叠
        root.setSpacing(9)

        root.addLayout(self._build_toolbar())

        # 环形 + 剩余额度
        top = QHBoxLayout()
        top.setSpacing(16)
        self._ring = RingProgress(size=84, thickness=9)
        top.addWidget(self._ring, 0, Qt.AlignmentFlag.AlignVCenter)

        box = QVBoxLayout()
        box.setSpacing(3)
        cap_row = QHBoxLayout()
        cap_row.setSpacing(8)
        cap = QLabel("本月剩余额度")
        cap.setObjectName("MetricLabel")
        cap_row.addWidget(cap)
        self._badge_source = Badge("等待数据", theme.TEXT_FAINT)
        cap_row.addWidget(self._badge_source)
        cap_row.addStretch(1)
        box.addLayout(cap_row)

        self._left = QLabel("--")
        self._left.setObjectName("Big")
        self._used = QLabel("--")
        self._used.setObjectName("Sub")
        box.addWidget(self._left)
        box.addWidget(self._used)
        box.addStretch(1)
        top.addLayout(box, 1)

        self._acct = QLabel("")
        self._acct.setObjectName("Sub")
        self._acct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        top.addWidget(self._acct, 0)
        root.addLayout(top)

        self._bar = _styled_bar()
        root.addWidget(self._bar)

        # 今日 / 日预算
        today_row = QHBoxLayout()
        self._today_label = QLabel("今日 -- / -- GB")
        self._today_label.setObjectName("Sub")
        self._surge = QLabel("")
        self._surge.setObjectName("Sub")
        self._surge.setStyleSheet(f"color: {theme.QUOTA_RED};")
        today_row.addWidget(self._today_label)
        today_row.addStretch(1)
        today_row.addWidget(self._surge)
        root.addLayout(today_row)
        self._bar_today = _styled_bar()
        root.addWidget(self._bar_today)

        # 近 30 天柱状图
        chart_lbl = QLabel("近 30 天日用量")
        chart_lbl.setObjectName("MetricLabel")
        root.addWidget(chart_lbl)
        self._bars = BarChart()
        # 112 是给网格留的实用下限：减去 PAD_T/PAD_B 后还有 ~84px 绘图区，
        # 四五条网格线看得清。再高就是白占垂直空间，把下面的热力图顶出可视区
        self._bars.setMinimumHeight(112)
        root.addWidget(self._bars, 1)

        # 当月热力图
        heat_row = QHBoxLayout()
        heat_lbl = QLabel("本月每日用量")
        heat_lbl.setObjectName("MetricLabel")
        heat_row.addWidget(heat_lbl)
        heat_row.addStretch(1)
        legend = QLabel("低 · · · 高　（越过日预算的日子标红）")
        legend.setObjectName("Faint")
        heat_row.addWidget(legend)
        root.addLayout(heat_row)
        self._heat = MonthHeatmap()
        root.addWidget(self._heat, 0, Qt.AlignmentFlag.AlignLeft)

        # 指标卡片 3x2
        grid = QGridLayout()
        grid.setSpacing(8)
        self._c_today = _MetricCard("今日用量")
        self._c_avg = _MetricCard("近 7 日日均")
        self._c_forecast = _MetricCard("预测月底")
        self._c_balance = _MetricCard("账户余额")
        self._c_v6 = _MetricCard("IPv6（免费）")
        self._c_gap = _MetricCard("口径差值")
        for i, c in enumerate((self._c_today, self._c_avg, self._c_forecast,
                               self._c_balance, self._c_v6, self._c_gap)):
            grid.addWidget(c, i // 3, i % 3)
        root.addLayout(grid)

        self._foot = QLabel("")
        self._foot.setObjectName("Faint")
        root.addWidget(self._foot)

    def _build_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self._btn_refresh = QPushButton("立即刷新额度")
        self._btn_refresh.setToolTip("不等下一轮轮询，马上重新查一次")
        self._btn_refresh.clicked.connect(self.refreshRequested.emit)
        row.addWidget(self._btn_refresh)

        self._btn_devices = QPushButton("设备管理")
        self._btn_devices.setToolTip("查看当前账号在线的终端，可以把陌生设备踢下线")
        self._btn_devices.clicked.connect(self.openDevicesRequested.emit)
        row.addWidget(self._btn_devices)

        row.addStretch(1)

        self._lb_devices = QLabel("在线终端 --")
        self._lb_devices.setObjectName("Sub")
        row.addWidget(self._lb_devices)
        return row

    # ---------------------------------------------------------------- 更新
    def set_device_count(self, count: int, limit: int = 0) -> None:
        self._online_devices = count
        if limit > 0 and count > limit:
            self._lb_devices.setText(f"在线终端 {count} 台 · 超过上限 {limit}")
            self._lb_devices.setStyleSheet(f"color:{theme.QUOTA_RED};")
        else:
            self._lb_devices.setText(f"在线终端 {count} 台")
            self._lb_devices.setStyleSheet("")

    def update_snapshot(self, snap: Snapshot) -> None:
        if not snap.online:
            self._left.setText("--")
            self._used.setText("请连接校园网并完成认证")
            self._badge_source.set_badge("离线", theme.TEXT_FAINT)
            self._ring.set_percent(0)
            self._ring.set_color(theme.QUOTA_GRAY)
            self._bar.setValue(0)
            self._today_label.setText("今日 -- / -- GB")
            self._bar_today.setValue(0)
            self._acct.setText(snap.error or "未连接校园网")
            self._acct.setStyleSheet(f"color:{theme.TEXT_FAINT};font-size:11px;")
            self.statusChanged.emit("校园网：离线")
            return

        q = self.quota
        pct_left = q.left_percent(snap)
        left_gb = q.left_gb(snap)
        color = theme.quota_color(pct_left)

        self._badge_source.set_badge(
            "接口口径" if snap.source == SOURCE_API else "门户口径",
            SOURCE_COLORS.get(snap.source, theme.TEXT_FAINT),
        )
        self._badge_source.setToolTip(
            "接口口径：ePortal loadUserFlow 的 v4，与自助服务权威账本一致\n"
            if snap.source == SOURCE_API else
            "接口这次没查到，暂用门户首页的 flow 兜底 —— 它是滞后口径，数字偏小\n"
        )

        session_acct = snap.account or ""
        cfg_acct = (self.cfg.campus.account or "").strip()
        acct_text = session_acct or cfg_acct or "未知"
        tip = ""
        if cfg_acct and session_acct and cfg_acct != session_acct:
            # 计费流量取自当前网络会话账号（门户首页），与设置账号不一致时明示，
            # 避免"改了账号刷新却没变化"的困惑（换账号需在校园网重新认证）。
            acct_text += " · ⚠ 非设置账号"
            tip = (f"计费流量来自当前会话账号 {session_acct}，设置中的账号是 {cfg_acct}。\n"
                   f"如需监控新账号，请先注销旧账号并重新认证登录。")
        self._acct.setText(f"{acct_text} · 在线")
        self._acct.setToolTip(tip)
        self._acct.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:12px;")

        if left_gb < 0:
            self._left.setText(f"已超额 {-left_gb:.2f} GB")
            self._left.setStyleSheet(f"color:{theme.QUOTA_BAD};font-size:26px;font-weight:600;")
        else:
            self._left.setText(f"{left_gb:.2f} GB")
            self._left.setStyleSheet("")
        self._used.setText(
            f"已用 {snap.used_gb:.2f} GB / {q.quota_gb:.0f} GB"
            f"（{q.used_percent(snap):.1f}%）"
        )
        self._ring.set_percent(pct_left)
        self._ring.set_color(color)
        self._bar.setValue(int(q.used_percent(snap) * 10))
        _tint(self._bar, color)

        # 今日 / 日预算
        if self.stats:
            today = self.stats.today_used_gb()
            budget = self.stats.daily_budget_gb()
            self._today_label.setText(f"今日 {today:.2f} GB / 预算 {budget:.2f} GB")
            ratio = (today / budget) if budget > 0 else 0
            self._bar_today.setValue(int(min(1.0, ratio) * 1000))
            _tint(self._bar_today, theme.QUOTA_RED if ratio > 1 else theme.QUOTA_GREEN)

            is_surge, t, base = self.stats.surge()
            self._surge.setText(f"突增 {t:.1f}GB（均值 {base:.1f}）" if is_surge else "")
            self._c_today.set_value(f"{today:.2f} GB", theme.QUOTA_RED if is_surge else None)
            self._c_avg.set_value(f"{self.stats.avg_gb(7):.2f} GB")
            fc = self.stats.forecast_gb(snap)
        else:
            fc = snap.used_gb

        over = q.over_fee(fc)
        if fc > q.quota_gb:
            self._c_forecast.set_value(f"{fc:.1f} GB", theme.QUOTA_RED)
            self._c_forecast.set_label(f"预测月底（超额 ¥{over:.2f}）")
        else:
            self._c_forecast.set_value(f"{fc:.1f} GB")
            self._c_forecast.set_label("预测月底（不会超额）")

        bal_red = snap.balance_yuan < self.cfg.campus.warn_balance_yuan
        self._c_balance.set_value(f"¥{snap.balance_yuan:.2f}",
                                  theme.QUOTA_RED if bal_red else None)
        self._c_balance.set_label(
            f"账户余额（可购 {q.buyable_gb(snap):.2f} GB）" if not bal_red
            else "账户余额 · 偏低"
        )

        self._c_v6.set_value(f"{snap.v6_gb:.2f} GB")
        self._c_gap.set_value(f"{snap.gap_gb:+.2f} GB")
        self._c_gap.set_label("口径差值（接口 − 门户 flow）")

        h, m = divmod(snap.minutes, 60)
        self._foot.setText(
            f"已用时长 {h} 小时 {m} 分　·　IPv4 {snap.v4ip or '-'}　·　"
            f"超额单价 ¥{q.price_per_gb:.2f}/GB　·　"
            f"门户 flow 口径 {snap.flow_gb:.2f} GB"
        )
        self.statusChanged.emit(f"校园网：剩余 {left_gb:.1f} GB")

    def update_history(self, hist: list[dict]) -> None:
        """刷新近 30 天柱状图 + 本月热力图。"""
        self._refresh_heatmap()
        days = [(h["day"][5:], h["used_gb"]) for h in hist]
        budget = self.stats.daily_budget_gb() if self.stats else 0.0
        self._bars.set_data(days, budget)

    def apply_settings(self) -> None:
        """设置里改了额度参数后重新吸收。"""
        self.quota = self.cfg.quota()
        if self.stats:
            self.stats.quota = self.quota
            self.stats.surge_factor = self.cfg.campus.surge_factor
        self._refresh_heatmap()

    def _refresh_heatmap(self) -> None:
        """用 daily 表里本月的记录刷新热力图，缺失日期自动补空。"""
        if not self.stats:
            return
        now = date.today()
        month = now.strftime("%Y-%m")
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        mapping = {
            int(r["day"][8:]): r["used_gb"]
            for r in self.stats.rows
            if r["month"] == month and r["used_gb"] > 0
        }
        self._heat.set_data(mapping, self.stats.daily_budget_gb(),
                            month=month, days_in_month=days_in_month,
                            today_day=now.day)

    def summary_for_tab(self) -> str:
        """给标签页标题用的一行摘要。"""
        if not self.stats:
            return "校园网额度"
        return f"校园网额度 · 今日 {self.stats.today_used_gb():.2f} GB"
