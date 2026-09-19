"""延迟监测页：目标管理 + 实时曲线 + 事件日志 + 网络诊断。

从 NetPing 的主窗口整体搬过来，改动只有三处：
  * 外层壳（标题、设置、退出、标签页）交给 MainWindow，本页只管延迟内容；
  * 悬浮窗开关上移到主窗口标题栏（它是全局的，两个页面都要能开关）；
  * 时间范围下拉框挪进来（只影响本页曲线）。
"""
from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QGuiApplication,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.config import CHART_WINDOWS, Config, Target
from core.net.engine import Engine, Event
from core.net.stats import Snapshot, StatsHub

from . import theme
from .chart import LineChart
from .diagnose import DiagnoseDialog
from .target_dialog import TargetDialog
from .widgets import Badge, WrapLabel, dot_pixmap

# 窗口最小宽度。徽章/状态行放在独立的第二行，所以这个宽度足够它们完整显示；
# 状态行超长会自动省略（见 _set_sub），徽章里的程序名也会在 core 层截断。
MIN_W = 820
# 状态行最长宽度：它只是播报，不让它把窗口撑宽
SUB_MAX_W = 320


def interval_text(intervals: list[float]) -> str:
    """把实际生效的探测间隔拼成一句人话。

    以前这里直接印配置里的全局 interval，但目标可以各自设不同间隔
    （编辑对话框里能单独改），于是界面上的"每 1 秒一次"经常是假的。
    现在按目标的真实值播报。
    """
    if not intervals:
        return "未监测"
    if len(intervals) == 1:
        return f"每 {intervals[0]:g} 秒一次"
    if len(intervals) <= 3:
        return "每 " + "/".join(f"{v:g}" for v in intervals) + " 秒一次"
    return f"每 {intervals[0]:g}–{intervals[-1]:g} 秒一次"


# ---------------------------------------------------------------- 目标操作菜单

def build_target_menu(parent: QWidget, target: Target,
                      enabled: bool) -> tuple[QMenu, dict[str, QAction]]:
    """构造目标操作菜单（只构造不弹出），返回 (菜单, {动作名: QAction})。

    和 exec 分开是为了能在测试里直接检查菜单内容 —— 弹出是阻塞的，测不了。
    """
    menu = QMenu(parent)
    acts: dict[str, QAction] = {}
    acts["edit"] = menu.addAction("编辑…")
    acts["toggle"] = menu.addAction("停用（先不监测）" if enabled else "启用")
    acts["copy"] = menu.addAction("复制地址")
    # 判 host 而不是 addr：addr 对 TCP 目标会拼成 ":443"，没有地址时也是"真"的
    acts["copy"].setEnabled(bool(target.host))
    menu.addSeparator()
    acts["remove"] = menu.addAction("删除目标")
    return menu, acts


def pick_target_action(parent: QWidget, target: Target, enabled: bool,
                       global_pos: QPoint) -> str | None:
    """弹出目标操作菜单，返回动作名：edit / toggle / copy / remove；取消返回 None。

    单独抽出来是因为有两个地方要用同一份菜单（左栏的列表行、右栏的图表卡），
    分头写迟早会漏掉一项。
    """
    menu, acts = build_target_menu(parent, target, enabled)
    chosen = menu.exec(global_pos)
    for name, act in acts.items():
        if chosen is act:
            return name
    return None


class TargetMenuMixin:
    """给"代表某个目标"的控件统一挂上操作菜单。

    子类必须自己声明 editRequested / toggleRequested / removeRequested 三个信号
    （PySide6 只允许 QObject 子类声明 Signal，所以这里只能用 mixin 提供方法），
    并提供 tid 与 _target 两个属性。
    """

    def _open_target_menu(self, global_pos: QPoint) -> None:
        action = pick_target_action(self, self._target, self._enabled, global_pos)
        if action == "edit":
            self.editRequested.emit(self.tid)
        elif action == "toggle":
            self.toggleRequested.emit(self.tid)
        elif action == "copy":
            QGuiApplication.clipboard().setText(self._target.addr)
        elif action == "remove":
            self.removeRequested.emit(self.tid)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """右键任意位置都能操作。

        之前只在行尾放了一个 22px 的「⋯」按钮，而左栏底下的提示写着
        "右键目标可编辑／停用／删除" —— 右键事件压根没实现，用户找不到删除入口。
        """
        self._open_target_menu(event.globalPos())


# ---------------------------------------------------------------- 目标行（左栏）

class TargetRow(TargetMenuMixin, QFrame):
    toggleRequested = Signal(str)
    editRequested = Signal(str)
    removeRequested = Signal(str)
    focusRequested = Signal(str)

    def __init__(self, target: Target, parent=None):
        super().__init__(parent)
        self.tid = target.id
        self._target = target
        self._enabled = target.enabled
        self.setObjectName("Card")
        self.setFixedHeight(56)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 8, 6)
        lay.setSpacing(8)

        self.lb_dot = QLabel()
        self.lb_dot.setFixedWidth(12)
        lay.addWidget(self.lb_dot)

        mid = QVBoxLayout()
        mid.setSpacing(1)
        self.lb_name = QLabel()
        self.lb_name.setFont(self._name_font())
        self.lb_addr = QLabel(f"{target.kind_label} · {target.addr}")
        self.lb_addr.setObjectName("Faint")
        mid.addWidget(self.lb_name)
        mid.addWidget(self.lb_addr)
        lay.addLayout(mid, 1)
        self.set_name(target.name)
        self.set_enabled_state(target.enabled)

        self.lb_rtt = QLabel("--")
        self.lb_rtt.setStyleSheet(f"font-size:14px;font-weight:600;color:{theme.TEXT_DIM};")
        self.lb_rtt.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.lb_rtt)

        self.btn_menu = QPushButton("⋯")
        self.btn_menu.setFixedSize(22, 22)
        self.btn_menu.setStyleSheet("border:none;padding:0;color:#98a1b0;font-size:14px;")
        self.btn_menu.setToolTip("目标操作：编辑 / 停用 / 删除（右键这一行也一样）")
        self.btn_menu.setCursor(Qt.CursorShape.ArrowCursor)
        self.btn_menu.clicked.connect(self._menu)
        lay.addWidget(self.btn_menu)

    def _menu(self) -> None:
        self._open_target_menu(
            self.btn_menu.mapToGlobal(QPoint(0, self.btn_menu.height()))
        )

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """左键点一下 → 滚到右边对应的图表。右键留给菜单。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.focusRequested.emit(self.tid)
        super().mouseReleaseEvent(event)

    def set_enabled_state(self, enabled: bool) -> None:
        self._enabled = enabled
        self.lb_name.setStyleSheet("" if enabled else f"color:{theme.TEXT_FAINT};")

    @staticmethod
    def _name_font() -> QFont:
        return theme.ui_font(12.5, bold=True)

    def set_name(self, name: str) -> None:
        fm = QFontMetricsF(self._name_font())
        self.lb_name.setText(fm.elidedText(name, Qt.TextElideMode.ElideRight, 146.0))
        self.lb_name.setToolTip(name)

    def set_addr(self, text: str) -> None:
        fm = QFontMetricsF(self.lb_addr.font())
        self.lb_addr.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, 150.0))
        self.lb_addr.setToolTip(text)

    def update_snapshot(self, enabled: bool, snap: Snapshot) -> None:
        self.set_enabled_state(enabled)
        color = theme.GRADE_COLORS.get(snap.grade, theme.GRADE_COLORS["unknown"])
        self.lb_dot.setPixmap(dot_pixmap(color if enabled else "#c9d0dc", 10))
        if not enabled:
            self.lb_rtt.setText("已停用")
            self.lb_rtt.setStyleSheet(f"font-size:12px;color:{theme.TEXT_FAINT};")
            return
        if snap.last is None:
            text = "超时" if snap.sent else "--"
        else:
            text = f"{snap.last:.0f}" if snap.last >= 1 else "<1"
        self.lb_rtt.setText(text)
        self.lb_rtt.setStyleSheet(f"font-size:14px;font-weight:600;color:{color};")

        def f1(v):
            return "--" if v is None else f"{v:.0f}"

        self.lb_rtt.setToolTip(
            f"最低 {f1(snap.mn)} / 平均 {f1(snap.avg)} / 最高 {f1(snap.mx)} ms\n"
            f"抖动 {f1(snap.jitter)} ms · 丢包 {snap.loss:.1f}%"
        )


# ---------------------------------------------------------------- 图表卡

class ChartCard(TargetMenuMixin, QFrame):
    toggleRequested = Signal(str)
    editRequested = Signal(str)
    removeRequested = Signal(str)

    def __init__(self, target: Target, parent=None):
        super().__init__(parent)
        self.tid = target.id
        self.setObjectName("Card")
        self._target = target          # 菜单 mixin 用这个名字
        self._enabled = target.enabled

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.lb_dot = QLabel()
        self.lb_dot.setFixedWidth(12)
        head.addWidget(self.lb_dot)
        self.lb_name = QLabel(target.name)
        self.lb_name.setObjectName("Title")
        head.addWidget(self.lb_name)
        self.lb_addr = QLabel(f"{target.kind_label} · {target.addr} · 每 {target.interval:g}s")
        self.lb_addr.setObjectName("Sub")
        head.addWidget(self.lb_addr)
        head.addStretch(1)
        self.lb_badge = Badge("采集中", theme.TEXT_FAINT)
        head.addWidget(self.lb_badge)
        self.lb_big = QLabel("--")
        self.lb_big.setStyleSheet(f"font-size:22px;font-weight:600;color:{theme.TEXT_FAINT};")
        self.lb_big.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.lb_big)
        lay.addLayout(head)

        self.chart = LineChart(target.color)
        lay.addWidget(self.chart, 1)

        # 统计行是两行文字，卡片被压矮时普通 QLabel 会被裁到只剩半行（见 ui/widgets.py）
        self.lb_stats = WrapLabel("--")
        self.lb_stats.setObjectName("Sub")
        lay.addWidget(self.lb_stats)

    def set_title(self, target: Target) -> None:
        self._target = target
        self.lb_name.setText(target.name)
        self.lb_addr.setText(f"{target.kind_label} · {target.addr} · 每 {target.interval:g}s")
        self.chart.set_color(target.color)
        self.lb_dot.setPixmap(dot_pixmap(target.color, 10))

    def update_snapshot(self, values: list[float | None], samples, span: float,
                        snap: Snapshot, total: Snapshot, enabled: bool) -> None:
        self.chart.set_data(samples, span)
        self._enabled = enabled

        if not enabled:
            self.lb_dot.setPixmap(dot_pixmap("#c9d0dc", 10))
            self.lb_big.setText("已停用")
            self.lb_big.setStyleSheet(f"font-size:18px;color:{theme.TEXT_FAINT};")
            self.lb_badge.set_badge("已停用", "#98a1b0")
            self.lb_stats.setText("该目标已停用。")
            return

        color = theme.GRADE_COLORS.get(snap.grade, theme.GRADE_COLORS["unknown"])
        self.lb_dot.setPixmap(dot_pixmap(self._target.color, 10))
        self.lb_badge.set_badge(theme.GRADE_LABEL[snap.grade], color)

        if snap.last is None:
            self.lb_big.setText("超时" if snap.sent else "--")
            big_color = theme.OV_LOSS if snap.sent else theme.TEXT_FAINT
            self.lb_big.setStyleSheet(f"font-size:20px;font-weight:600;color:{big_color};")
        else:
            self.lb_big.setText(f"{snap.last:.0f} ms" if snap.last >= 1 else "<1 ms")
            self.lb_big.setStyleSheet(f"font-size:22px;font-weight:600;color:{color};")

        def fmt(v, unit="ms"):
            if v is None:
                return f'<span style="color:{theme.TEXT_DIM}">--</span>'
            return f"<b>{v:.0f}</b> {unit}"

        def tag(label: str) -> str:
            return f'<span style="color:{theme.TEXT_FAINT}">{label}</span>'

        loss_color = theme.OV_LOSS if snap.loss >= 1.0 else theme.TEXT_DIM
        total_color = theme.OV_LOSS if total.loss >= 1.0 else theme.TEXT_DIM
        bits = [
            f"{tag('最低')} {fmt(snap.mn)}",
            f"{tag('平均')} {fmt(snap.avg)}",
            f"{tag('最高')} {fmt(snap.mx)}",
            f"{tag('抖动')} {fmt(snap.jitter)}",
            f"{tag('P95')} {fmt(snap.p95)}",
            f'{tag("窗口丢包")} <b style="color:{loss_color}">{snap.loss:.1f}%</b>',
            f'{tag("累计丢包")} <b style="color:{total_color}">{total.loss:.1f}%</b>'
            f' <span style="color:{theme.TEXT_FAINT}">({total.lost}/{total.sent})</span>',
        ]
        self.lb_stats.setText(" &nbsp;·&nbsp; ".join(bits))


# ---------------------------------------------------------------- 页面

class LatencyPage(QWidget):
    """延迟监测页。"""

    statusChanged = Signal(str)     # 供主窗口在标签上显示一行摘要
    # 主开关（持久化）切换请求：本页不直接写 cfg.settings["latency_enabled"]，
    # 交给 app 层统一处理（启动/停止引擎 + 落盘 + 通知托盘），避免两条生效路径。
    enableRequested = Signal(bool)
    # 事件搬运：主窗口把两个子系统的日志汇到同一个框里显示，所以本页默认不自建日志。
    # with_log=True 时保留自己那份（单独跑本页调试用）。
    logRequested = Signal(str, str)   # (正文, 严重度)
    openDevicesRequested = Signal()   # 本页不产生，仅保持与校园网页同形

    def __init__(self, config: Config, hub: StatsHub, engine: Engine, overlay=None,
                 with_log: bool = True, parent=None):
        super().__init__(parent)
        self._with_log = with_log
        self.cfg = config
        self.hub = hub
        self.engine = engine
        self.overlay = overlay
        # 延迟监测主开关：默认关闭（用户手动开启后持久化）。
        # 与「暂停」不同：暂停是本次会话内的临时停，主开关跨启动生效。
        self._master_on = bool(config.settings.get("latency_enabled"))
        self._cards: dict[str, ChartCard] = {}
        self._rows: dict[str, TargetRow] = {}
        self._cache: dict[str, tuple] = {}      # tid -> (渲染键, Snapshot, 累计 Snapshot)
        self._last_event_ts = 0.0
        self.env = None                          # 运行环境检查结果（直连 / 经代理）
        self._env_pending = None
        self._env_busy = False
        self._sub_color = None
        self._sub_text: tuple[str, str] | None = None

        self.setMinimumWidth(MIN_W)
        self._build_ui()
        self._rebuild_targets()
        self._sync_engine_ui()           # 按主开关初值摆好按钮与播报（默认是关闭态）
        self._start_env_check()          # 界面建好后再起，结果回来就能直接落盘

        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    # ------------------------------------------------------------ 构建

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        root.addLayout(self._build_toolbar())
        root.addLayout(self._build_status_row())

        body = QHBoxLayout()
        body.setSpacing(10)
        body.addWidget(self._build_left_panel(), 0)
        body.addWidget(self._build_chart_panel(), 1)
        root.addLayout(body, 1)

        self.txt_events = QTextEdit()
        self.txt_events.setObjectName("Events")
        self.txt_events.setReadOnly(True)
        self.txt_events.setFixedHeight(96)
        self.txt_events.document().setMaximumBlockCount(400)
        self.txt_events.setPlaceholderText("暂无事件")
        if self._with_log:
            root.addWidget(self.txt_events)

    def _build_toolbar(self) -> QHBoxLayout:
        """本页的操作条：时间范围 + 暂停 + 诊断 + 导出 + 添加目标。

        徽章和状态行刻意不放在这一行 —— 按钮已经把宽度吃掉大半，
        再把两个徽章和一整句状态挤进来，窗口一窄它们就会被压扁（字被裁掉）。
        """
        row = QHBoxLayout()
        row.setSpacing(8)

        self.cb_window = QComboBox()
        for label, sec in CHART_WINDOWS:
            self.cb_window.addItem(label, sec)
        self.cb_window.setFixedWidth(130)
        idx = next((i for i, (_l, s) in enumerate(CHART_WINDOWS)
                    if s == self.cfg.settings.get("chart_window", 300)), 1)
        self.cb_window.setCurrentIndex(idx)
        self.cb_window.setToolTip("曲线横轴显示多长的时间范围")
        self.cb_window.currentIndexChanged.connect(self._on_window_changed)
        row.addWidget(self.cb_window)

        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setToolTip("暂停/恢复延迟探测（校园网额度监测不受影响）")
        self.btn_pause.clicked.connect(self._toggle_pause)
        row.addWidget(self.btn_pause)

        self.btn_diag = QPushButton("网络诊断")
        self.btn_diag.clicked.connect(self._open_diagnose)
        row.addWidget(self.btn_diag)

        self.btn_export = QPushButton("导出记录")
        self.btn_export.setToolTip("把本次监测的原始数据存成 CSV，方便复盘或发给宽带客服")
        self.btn_export.clicked.connect(self._export_csv)
        row.addWidget(self.btn_export)

        row.addStretch(1)

        self.btn_add = QPushButton("添加目标")
        self.btn_add.setObjectName("Primary")
        self.btn_add.clicked.connect(self._add_target)
        row.addWidget(self.btn_add)
        return row

    def _build_status_row(self) -> QHBoxLayout:
        """第二行：整体评价 + 直连/经代理 + 一句状态播报。"""
        row = QHBoxLayout()
        row.setSpacing(8)

        self.lb_overall = Badge()
        row.addWidget(self.lb_overall)

        # 直连 / 经代理 —— 这两个状态下延迟数字的含义不同，必须让用户一眼看到
        self.lb_mode = Badge("检测中…", theme.TEXT_FAINT)
        self.lb_mode.setToolTip("正在检测当前流量是直连还是走代理/加速器")
        row.addWidget(self.lb_mode)

        self.lb_sub = QLabel("正在启动…")
        self.lb_sub.setObjectName("Sub")
        row.addWidget(self.lb_sub)
        row.addStretch(1)
        return row

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Card")
        panel.setFixedWidth(276)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        lb = QLabel("监测目标")
        lb.setObjectName("SectionTitle")
        lay.addWidget(lb)

        self.targets_box = QVBoxLayout()
        self.targets_box.setSpacing(6)
        self.targets_box.addStretch(1)
        lay.addLayout(self.targets_box)
        return panel

    def _build_chart_panel(self) -> QWidget:
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        holder = QWidget()
        self.chart_box = QVBoxLayout(holder)
        self.chart_box.setContentsMargins(0, 0, 4, 0)
        self.chart_box.setSpacing(10)
        self.chart_box.addStretch(1)
        self.scroll.setWidget(holder)
        return self.scroll

    # ------------------------------------------------------------ 目标同步

    def _rebuild_targets(self) -> None:
        """目标集合发生增删时才需要走这里（编辑/启停不用重建控件）。

        引擎走 sync() 而不是 start()：只动变化的那一个目标线程，
        其它目标的采样不中断，界面也不会卡。
        """
        self.engine.sync(self.cfg.enabled_targets())
        self._sync_hub()
        self._cache.clear()

        # 左栏
        while self.targets_box.count():
            item = self.targets_box.takeAt(0)
            w = item.widget()
            if w:
                w.hide()          # 从布局里摘掉的控件不会自动隐藏，先藏起来免得残留
                w.deleteLater()
        self._rows.clear()
        for target in self.cfg.targets:
            row = TargetRow(target)
            row.editRequested.connect(self._edit_target)
            row.removeRequested.connect(self._remove_target)
            row.toggleRequested.connect(self._toggle_target)
            row.focusRequested.connect(self.focus_target)
            self.targets_box.addWidget(row)
            self._rows[target.id] = row
        self.targets_box.addStretch(1)

        # 右栏
        while self.chart_box.count():
            item = self.chart_box.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.deleteLater()
        self._cards.clear()
        for target in self.cfg.targets:
            card = ChartCard(target)
            card.editRequested.connect(self._edit_target)
            card.removeRequested.connect(self._remove_target)
            card.toggleRequested.connect(self._toggle_target)
            self.chart_box.addWidget(card)
            self._cards[target.id] = card
        self.chart_box.addStretch(1)

        self.cb_window.setEnabled(bool(self.cfg.targets))
        self._refresh_static_texts()

    def _sync_hub(self) -> None:
        want = {t.id: t for t in self.cfg.targets}
        for tid in list(self.hub.targets):
            if tid not in want:
                del self.hub.targets[tid]
        for tid, target in want.items():
            if tid not in self.hub.targets:
                self.hub.register(tid, target.name, target.color)
            else:
                # 名字要用 hub.rename 同步过去：事件日志和诊断结论里显示的是 stats 里的名字
                self.hub.rename(tid, target.name)
                self.hub.targets[tid].color = target.color

    def _refresh_static_texts(self) -> None:
        """把目标上的"静态文字"同步到界面（名字、地址、间隔、提示语）。

        编辑/启停目标时不重建控件，只调这个方法 —— 没有闪烁，也不用重排。
        """
        for target in self.cfg.targets:
            card = self._cards.get(target.id)
            if card:
                card.set_title(target)
            row = self._rows.get(target.id)
            if row:
                row.set_name(target.name)
                row.set_addr(f"{target.kind_label} · {target.addr}")
                row.set_enabled_state(target.enabled)
                if not target.enabled and target.role == "gateway":
                    row.setToolTip(
                        "你的网关不响应 ICMP（校园网和不少路由器都这样），已默认停用。\n"
                        "想监测本地链路：右键启用，或改成 TCP 方式、端口填路由器管理页的 80。"
                    )
                elif target.role == "gateway":
                    row.setToolTip("本地基准：这段不经过运营商，用来判断是自己家/宿舍网络的问题。")
                elif target.role == "baseline":
                    row.setToolTip("公网基准：国内通、境外不通 → 出境线路问题；两个都丢 → 运营商问题。")
                else:
                    row.setToolTip("关注的目标。右键可编辑／停用／删除。")

    def roles(self) -> dict[str, str]:
        return {t.id: t.role for t in self.cfg.targets}

    # ------------------------------------------------------------ 目标操作

    def _add_target(self) -> None:
        dlg = TargetDialog(None, self._color_choices(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        target = dlg.result_target()
        self.cfg.targets.append(target)
        self.cfg.save()
        self._rebuild_targets()
        self._append_note(f"已添加目标「{target.name}」（{target.addr}）")

    def _color_choices(self) -> list[str]:
        """候选颜色，把当前还没被占用的那个排在最前。

        对话框默认选第一项，所以这样新加的目标会自动拿到一个没用过的颜色，
        不会跟已有曲线撞色。
        """
        from core.config import PALETTE

        nxt = self.cfg.next_color()
        return [nxt] + [c for c in PALETTE if c != nxt]

    def _edit_target(self, tid: str) -> None:
        """编辑目标。

        不重建控件也不重启线程：对话框是**原地改** Target 对象的，
        而探测线程每轮都会重新读一遍参数，所以改完下一轮就生效。
        只有"测的东西变了"（地址/端口/探测方式）才需要丢掉旧样本 ——
        那些样本是另一个主机的，混在统计里会算错累计丢包。
        """
        target = next((t for t in self.cfg.targets if t.id == tid), None)
        if target is None:
            return
        before = (target.host, target.port, target.kind)
        dlg = TargetDialog(target, self._color_choices(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.cfg.save()

        if (target.host, target.port, target.kind) != before:
            st = self.hub.get(tid)
            if st is not None:
                st.clear()
            self._cache.pop(tid, None)          # 旧曲线清掉，别让新目标接着画老数据
            self._append_note(f"「{target.name}」的监测地址已改为 {target.addr}，历史数据已重置")

        self.engine.sync(self.cfg.enabled_targets())
        self._sync_hub()
        self._cache.pop(tid, None)              # 名字/颜色可能变了，让它重画一次
        self._refresh_static_texts()
        self._refresh_ui()

    def _remove_target(self, tid: str) -> None:
        target = next((t for t in self.cfg.targets if t.id == tid), None)
        if target is None:
            return
        if QMessageBox.question(
            self, "删除目标", f"确定不再监测「{target.name}」？"
        ) != QMessageBox.StandardButton.Yes:
            return
        self.cfg.targets = [t for t in self.cfg.targets if t.id != tid]
        st = self.hub.get(tid)
        if st:
            st.clear()
        self.cfg.save()
        self._rebuild_targets()
        self._append_note(f"已移除目标「{target.name}」")

    def _toggle_target(self, tid: str) -> None:
        target = next((t for t in self.cfg.targets if t.id == tid), None)
        if target is None:
            return
        target.enabled = not target.enabled
        self.cfg.save()
        # 不用重建：刷新键里带了 enabled，下一轮刷新自然会画成"已停用"的样子
        self.engine.sync(self.cfg.enabled_targets())
        self._refresh_static_texts()
        self._refresh_ui()

    # ------------------------------------------------------------ 运行控制

    def _toggle_pause(self) -> None:
        if not self._master_on:
            # 主开关关着时，页内唯一的启动入口 = 打开主开关（持久化），
            # 否则"点开了、重启又没了"会让人以为按钮是坏的。
            self.enableRequested.emit(True)
            return
        if self.engine.running:
            self.engine.stop()
            self._sync_engine_ui()
        else:
            self.engine.start(self.cfg.enabled_targets())
            self._sync_engine_ui()

    def pause(self, on: bool) -> None:
        """给托盘/主窗口用的显式暂停入口。"""
        if not self._master_on:
            # 主开关关着时：请求"暂停"无事可做（本来就停着）；
            # 请求"继续/恢复"视为打开主开关（持久化），否则托盘开关像坏的。
            if not on:
                self.enableRequested.emit(True)
            return
        if on and self.engine.running:
            self._toggle_pause()
        elif not on and not self.engine.running:
            self._toggle_pause()

    def is_paused(self) -> bool:
        return not (self._master_on and self.engine.running)

    def set_master_enabled(self, on: bool) -> None:
        """app 层改完主开关后回调这里刷新界面（含按钮文案与播报）。"""
        self._master_on = bool(on)
        if not on and self.engine.running:
            self.engine.stop()
        self._sync_engine_ui()      # 按钮三态 + 播报一起刷，漏了按钮就会停在旧文案

    def _sync_engine_ui(self) -> None:
        """按「主开关 + 引擎状态」刷按钮文案与状态行（三态：关闭/暂停/监测中）。"""
        if not self._master_on:
            self.btn_pause.setText("开启监测")
        elif self.engine.running:
            self.btn_pause.setText("暂停")
        else:
            self.btn_pause.setText("继续")
        self._refresh_static_texts()

    def _on_window_changed(self, _index: int) -> None:
        self.cfg.settings["chart_window"] = self.cb_window.currentData() or 300
        self.cfg.save()

    def _open_diagnose(self) -> None:
        dlg = DiagnoseDialog(self.engine, self.roles(), self.cfg.targets, self.env, self)
        dlg.show()
        self._diag_dialog = dlg

    def _export_csv(self) -> None:
        import csv

        stamp = time.strftime("%Y%m%d-%H%M%S")
        default = os.path.join(os.path.expanduser("~"), f"NetOne-{stamp}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "导出监测记录", default, "CSV 文件 (*.csv)")
        if not path:
            return
        rows = 0
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["时间", "目标", "主机", "探测方式", "RTT(ms)", "状态"])
                base = time.time()
                for target in self.cfg.targets:
                    st = self.hub.get(target.id)
                    if st is None:
                        continue
                    for ts, rtt in st.samples:
                        wall = base - (time.monotonic() - ts)
                        writer.writerow([
                            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(wall)),
                            target.name, target.host, target.kind_label,
                            "" if rtt is None else f"{rtt:.1f}",
                            "丢包" if rtt is None else "正常",
                        ])
                        rows += 1
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        if rows == 0:
            QMessageBox.information(self, "没有数据", "还没有采集到样本，先让监测跑一会儿。")
            return
        QMessageBox.information(self, "导出完成", f"已写入 {rows} 条记录：\n{path}")

    # ------------------------------------------------------------ 刷新循环

    def _tick(self) -> None:
        samples = self.engine.drain()
        if samples:
            events = self.engine.ingest(samples)
            for ev in events:
                self._append_event(ev)
        if self._env_pending is not None:
            report = self._env_pending
            self._env_pending = None
            if report is not None:
                self._apply_env(report)
        self._refresh_ui()

    def _start_env_check(self) -> None:
        """环境检查要跑 ipconfig/route/tasklist，约 1 秒 —— 放后台线程，别卡住启动。"""
        from core.net.envcheck import inspect as inspect_env

        def work() -> None:
            try:
                self._env_pending = inspect_env(full=True)
            except Exception:
                self._env_pending = None

        threading.Thread(target=work, daemon=True, name="envcheck").start()

        # 用户会中途开关加速器，所以定期复查（轻量模式，约 350ms，跳过 tasklist）
        self._env_timer = QTimer(self)
        self._env_timer.setInterval(45000)
        self._env_timer.timeout.connect(self._recheck_env)
        self._env_timer.start()

    def _recheck_env(self) -> None:
        if self._env_busy:
            return
        from core.net.envcheck import inspect as inspect_env

        self._env_busy = True

        def work() -> None:
            try:
                report = inspect_env(full=False)
                # 轻量模式拿不到进程名，把上一次的补回去，免得标签上的程序名闪掉
                if self.env is not None and not report.proxy_procs:
                    report.proxy_procs = list(self.env.proxy_procs)
                self._env_pending = report
            except Exception:
                self._env_pending = None

        threading.Thread(target=work, daemon=True, name="envcheck-light").start()

    def _apply_env(self, report) -> None:
        """环境检查结果落地：切换模式时记一条事件，方便对比开关加速器前后。"""
        prev_mode = self.env.mode if self.env is not None else None
        self.env = report
        self._env_busy = False

        self.lb_mode.set_badge(report.chip,
                               theme.GRADE_COLORS["good"] if not report.proxied else theme.ACCENT)
        self.lb_mode.setToolTip(report.chip_tip)

        if prev_mode is not None and prev_mode != report.mode:
            if report.proxied:
                text = f"流量切换为经代理/加速器转发（{report.chip}）—— 之后的延迟是游戏内实际体验值"
            else:
                text = "流量切回直连 —— 之后的延迟是线路本身的延迟"
            self._append_event(Event(time.monotonic(), "", "环境", "recovered", text, "info"))

    def _refresh_ui(self) -> None:
        """刷新界面。

        采样是 1Hz，但刷新循环跑 5Hz —— 所以按"数据是否变了"做跳过：
        数据没变就一个控件都不碰。Qt 重排富文本比想象中贵，
        挂机打游戏时这点开销不该跟游戏抢 CPU。
        """
        window = self.cb_window.currentData()
        overall_worst = 100.0
        warn_names = []

        for target in self.cfg.targets:
            st = self.hub.get(target.id)
            if st is None:
                continue

            last_ts = st.samples[-1][0] if st.samples else 0.0
            key = (len(st.samples), last_ts, window, target.enabled)
            hit = self._cache.get(target.id)
            if hit is not None and hit[0] == key:
                snap = hit[1]
            else:
                snap = st.snapshot(window)
                self._cache[target.id] = (key, snap, st.totals())
                samples = st.window(window)
                values = [rtt for _ts, rtt in samples]
                # 时间轴跨度：优先用选中的窗口，但刚启动数据不够一个窗口时按实际跨度来，
                # 否则曲线会被挤在右边一小条（选"全部数据"时跨度就是整段数据）。
                span = window
                if len(samples) >= 2:
                    actual = samples[-1][0] - samples[0][0]
                    if span is None or actual < span:
                        span = max(10.0, actual)
                if not span:
                    span = 60.0
                card = self._cards.get(target.id)
                if card:
                    card.update_snapshot(
                        values, samples, span, snap, self._cache[target.id][2], target.enabled
                    )
                row = self._rows.get(target.id)
                if row:
                    row.update_snapshot(target.enabled, snap)

            if target.enabled and snap.sent:
                overall_worst = min(overall_worst, snap.score)
                if snap.grade in ("bad", "poor"):
                    warn_names.append(target.name)

        grade = "unknown"
        if overall_worst < 101:
            grade = ("good" if overall_worst >= 80 else "fair" if overall_worst >= 60
                     else "poor" if overall_worst >= 40 else "bad")
        color = theme.GRADE_COLORS.get(grade, theme.GRADE_COLORS["unknown"])
        self.lb_overall.set_badge(f"整体 {theme.GRADE_LABEL[grade]}", color)

        # 副标题播报真正异常的情况；"经代理"由顶部的模式标签负责说明，不在这里重复
        env_warn = ""
        if self.env is not None:
            for severity, head, _detail in self.env.notes:
                if severity == "warn":
                    env_warn = head
                    break

        if not self._master_on:
            sub = "延迟监测已关闭 · 点「开启监测」或到设置里打开（校园网额度不受影响）"
            sub_color = theme.TEXT_DIM
        elif env_warn:
            sub, sub_color = env_warn, theme.GRADE_COLORS["fair"]
        elif not self.engine.running:
            sub, sub_color = "已暂停 · 点击「继续」恢复监测", theme.TEXT_DIM
        elif warn_names:
            sub = "网络异常：" + "、".join(warn_names[:3]) + ("…" if len(warn_names) > 3 else "")
            sub_color = theme.OV_LOSS
        else:
            n = len([t for t in self.cfg.targets if t.enabled])
            sub = f"正在监测 {n} 个目标 · {interval_text(self.cfg.active_intervals())}"
            sub_color = theme.TEXT_DIM

        self._set_sub(sub, sub_color)

    def _set_sub(self, text: str, color: str) -> None:
        """副标题。

        宽度设上限、超长直接省略：这行是纯播报，省略没关系；
        但要是让它自由生长，它会把标题栏的天然最小宽度顶上去，
        用户就没法把窗口拖窄了（窗口最小宽度是按标题栏算的）。
        """
        if self._sub_text == (text, color):
            return
        self._sub_text = (text, color)
        fm = QFontMetrics(self.lb_sub.font())
        shown = fm.elidedText(text, Qt.TextElideMode.ElideRight, SUB_MAX_W)
        if self.lb_sub.text() != shown:
            self.lb_sub.setText(shown)
        self.lb_sub.setToolTip(text if shown != text else "")
        if self._sub_color != color:
            self._sub_color = color
            self.lb_sub.setStyleSheet(f"color:{color};")
        self.statusChanged.emit(text)

    def _append_note(self, text: str, severity: str = "info") -> None:
        """记一条"操作类"事件，方便回看什么时候动过什么。"""
        self._append_event(Event(time.monotonic(), "", "操作", "info", text, severity))

    def _append_event(self, ev) -> None:
        self.append_event(ev.text, ev.severity)

    def append_event(self, text: str, severity: str = "info") -> None:
        """写一条事件到日志。

        日志框由主窗口持有（with_log=False）时不自绘，只把正文和严重度转发出去，
        避免同一句话在标签页底部和窗口底部各出现一次。
        """
        if self._with_log:
            color = {"info": theme.TEXT_DIM, "warn": theme.GRADE_COLORS["fair"],
                     "bad": theme.GRADE_COLORS["bad"]}.get(severity, theme.TEXT_DIM)
            ts = time.strftime("%H:%M:%S", time.localtime())
            self.txt_events.append(
                f'<span style="color:{theme.TEXT_FAINT}">{ts}</span> '
                f'<span style="color:{color}">{text}</span>'
            )
        self.logRequested.emit(text, severity)

    # ------------------------------------------------------------ 给悬浮窗的数据

    def overlay_rows(self, window: float = 60.0) -> list[dict]:
        out = []
        for target in self.cfg.targets:
            if not target.enabled:
                continue
            st = self.hub.get(target.id)
            if st is None:
                continue
            snap = st.snapshot(window)
            out.append({
                "tid": target.id,
                "name": target.name,
                "color": target.color,
                "rtt": snap.last,
                "loss": snap.loss,
                "jitter": snap.jitter,
                "grade": snap.grade,
                "status": st.last_status,
                "values": [rtt for _ts, rtt in st.window(window)],
            })
        return out

    def focus_target(self, tid: str) -> None:
        card = self._cards.get(tid)
        if card:
            self.scroll.ensureWidgetVisible(card, 0, 20)

    def clear_history(self) -> int:
        """清空所有目标的样本与事件日志。返回清掉的目标数。"""
        count = 0
        for target in self.cfg.targets:
            st = self.hub.get(target.id)
            if st is not None:
                st.clear()
                count += 1
        self.engine.clear_history()
        self._cache.clear()
        self.txt_events.clear()
        self._refresh_ui()
        return count

    def sync_chart_window(self) -> None:
        """把时间范围下拉框对齐到配置里的值（拦截信号，避免又写回去）。"""
        idx = next((i for i, (_l, s) in enumerate(CHART_WINDOWS)
                    if s == self.cfg.settings.get("chart_window")), 1)
        if self.cb_window.currentIndex() == idx:
            return
        self.cb_window.blockSignals(True)
        self.cb_window.setCurrentIndex(idx)
        self.cb_window.blockSignals(False)

    def apply_after_settings(self) -> None:
        """设置保存后让本页重新吸收配置（间隔/超时/时间范围可能都变了）。"""
        self.sync_chart_window()
        self._cache.clear()
        self._refresh_static_texts()
        self._refresh_ui()
