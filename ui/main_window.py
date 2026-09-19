"""NetOne 主窗口：一个外壳 + 三个标签页。

合并 NetPing（延迟/丢包监测）与 NetQuota（校园网流量额度）时，没有把两套界面揉
成一个巨型窗口，而是各自保留成独立一页（ui/page_latency.py / ui/page_campus.py），
本窗口只负责三件事：

1. 顶部一行全局操作（标题 / 悬浮窗开关 / 设置 / 退出）—— 两页共用的动作只出现一次；
2. QTabWidget 把「网络延迟 / 校园网额度 / 日志」三页装起来；
3. 底部一行跨页摘要。

跨页摘要不是装饰：看额度页时也得知道网络是不是断了，看延迟页时也得知道本月还剩
多少流量 —— 不然每次都要切过去看一眼，等于没合并。
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.config import MAIN_TITLE, Config
from core.net import history as latency_history
from ui import theme
from ui.page_campus import CampusPage
from ui.page_latency import LatencyPage
from ui.settings_dialog import SettingsDialog

# 延迟页自身最小宽 820，加上外层边距和标签栏，窗口最小宽取 880 才不会把左栏压扁
MIN_W = 880

# 标签页下标 -> last_tab 配置键（日志页也要记忆，见 _on_tab_changed / restore_tab）
TAB_KEYS = {0: "latency", 1: "campus", 2: "log"}


class MainWindow(QMainWindow):
    """主窗口。只做装配，业务都在两个页面里。"""

    overlayToggleRequested = Signal(bool)
    quitRequested = Signal()
    hiddenToTray = Signal()
    openDevicesRequested = Signal()      # 交给 app 层开设备对话框
    testNotifyRequested = Signal()       # 交给 app 层发一条测试通知
    settingsChanged = Signal(object)     # 被改动的分组名集合，app 层据此重启定时器

    def __init__(self, config: Config, hub, engine, campus_stats=None,
                 campus_store=None, overlay=None, parent=None):
        super().__init__(parent)
        self.cfg = config
        self.hub = hub
        self.engine = engine
        self.overlay = overlay

        self._sum_latency = "正在启动…"
        self._sum_campus = "正在读取额度…"
        self._sub_color: str | None = None
        # 开着的设置对话框（app 层显隐变化时要回头同步它，见 settings_dialog.
        # sync_overlay_visible）。非模态 + 单例：重复点设置只把已有的抬到前台。
        self.settings_dlg = None

        self.setWindowTitle(MAIN_TITLE)
        self.setStyleSheet(theme.qss())
        size = config.settings.get("window") or {}
        self.resize(int(size.get("w") or 1180), int(size.get("h") or 800))
        self.setMinimumWidth(MIN_W)

        self._build_ui(hub, engine, campus_stats, campus_store)

        # 最小高度不能拍脑袋写一个数（这里曾经写的是 600，而布局真实需要 800+）。
        # 窗口一旦被压到真实最小高度以下，QVBoxLayout 不会裁剪内容，而是把某个
        # 间距挤成负数 —— 相邻控件直接叠在一起（实测额度页的柱状图会盖住热力图
        # 标题，日期和"本月每日用量"糊成一团）。所以排完版再按实际值钉住。
        #
        # 用 singleShot 而不是当场取：此刻还没走过第一次布局，minimumSizeHint()
        # 拿到的还是未 polished 的值，会偏小。
        QTimer.singleShot(0, self._pin_min_height)

    def _pin_min_height(self) -> None:
        """把窗口最小高度钉到布局真实需要的高度上。"""
        need = self.minimumSizeHint().height()
        if need > self.minimumHeight():
            self.setMinimumHeight(need)

    # ------------------------------------------------------------ 构建

    def _build_ui(self, hub, engine, campus_stats, campus_store) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        root.addLayout(self._build_title_row())

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        # 日志框由本窗口持有，两页的事件都汇到这里（见 LatencyPage.append_event）
        self.latency = LatencyPage(self.cfg, hub, engine, overlay=self.overlay,
                                   with_log=False)
        self.campus = CampusPage(self.cfg, campus_stats, campus_store)

        self.tabs.addTab(self.latency, "网络延迟")
        self.tabs.addTab(self.campus, "校园网额度")

        # 共享事件日志独占第三个标签页（用户要求：别压在主界面底下）。
        # 由本窗口持有，两页的事件都汇到这里（见 LatencyPage.append_event）
        self.txt_events = QTextEdit()
        self.txt_events.setObjectName("Events")
        self.txt_events.setReadOnly(True)
        self.txt_events.document().setMaximumBlockCount(600)
        self.txt_events.setPlaceholderText("暂无事件")
        self.tabs.addTab(self.txt_events, "日志")

        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs, 1)

        root.addLayout(self._build_summary_row())

        self._connect()

    def _build_title_row(self) -> QHBoxLayout:
        """第一行：程序名 + 全局操作。

        只有真正两页共用的按钮才放这里。时间范围/暂停/诊断/导出/添加目标都属于
        延迟页自己的操作条，留在页内 —— 否则这一行会挤到十来个按钮。
        """
        head = QHBoxLayout()
        head.setSpacing(8)

        lb_title = QLabel("NetOne")
        lb_title.setObjectName("Title")
        head.addWidget(lb_title)
        head.addStretch(1)

        self.btn_overlay = QPushButton("悬浮窗")
        self.btn_overlay.setCheckable(True)
        self.btn_overlay.setChecked(bool(self.cfg.overlay_cfg.get("visible", True)))
        self.btn_overlay.setToolTip("显示/隐藏桌面悬浮窗（额度环 + 各目标延迟）")
        self.btn_overlay.toggled.connect(self.overlayToggleRequested.emit)
        head.addWidget(self.btn_overlay)

        self.btn_settings = QPushButton("设置")
        self.btn_settings.setToolTip("探测间隔、悬浮窗外观、校园网额度口径、提醒与自启动")
        self.btn_settings.clicked.connect(self._open_settings)
        head.addWidget(self.btn_settings)

        self.btn_quit = QPushButton("退出")
        self.btn_quit.setObjectName("Danger")
        self.btn_quit.setToolTip("真正退出程序（关闭本窗口只是收进托盘）")
        self.btn_quit.clicked.connect(self.quitRequested.emit)
        head.addWidget(self.btn_quit)
        return head

    def _build_summary_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.setContentsMargins(2, 0, 2, 0)

        self.lb_summary = QLabel("")
        self.lb_summary.setObjectName("Sub")
        row.addWidget(self.lb_summary, 1)
        return row

    def _connect(self) -> None:
        self.latency.statusChanged.connect(self._on_latency_status)
        self.latency.logRequested.connect(self.append_event)
        self.campus.statusChanged.connect(self._on_campus_status)
        self.campus.openDevicesRequested.connect(self.openDevicesRequested.emit)
        self._refresh_summary()

    # ------------------------------------------------------------ 摘要

    def _on_latency_status(self, text: str) -> None:
        self._sum_latency = text
        self._refresh_summary()

    def _on_campus_status(self, text: str) -> None:
        # CampusPage 播报的是"校园网：剩余 X GB"，这里只需要后半截
        self._sum_campus = text.split("：", 1)[-1] if "：" in text else text
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        text = f"网络  {self._sum_latency}     校园网  {self._sum_campus}"
        if self.lb_summary.text() != text:
            self.lb_summary.setText(text)

    def _on_tab_changed(self, index: int) -> None:
        # 记住用户停在哪一页，下次启动直接落回去（三个页都记，含日志页）
        key = TAB_KEYS.get(index, "latency")
        if self.cfg.settings.get("last_tab") != key:
            self.cfg.settings["last_tab"] = key
            self.cfg.save()

    def restore_tab(self) -> None:
        """按上次退出时的标签页定位（构建完成后再调用）。"""
        idx = {v: k for k, v in TAB_KEYS.items()}.get(
            str(self.cfg.settings.get("last_tab")), 0)
        if idx:
            self.tabs.setCurrentIndex(idx)

    def latency_status_text(self) -> str:
        """最近一次网络状态播报（托盘 tooltip 用）。"""
        return self._sum_latency

    def campus_status_text(self) -> str:
        return self._sum_campus

    def show_campus(self) -> None:
        self.tabs.setCurrentIndex(1)

    def show_latency(self) -> None:
        self.tabs.setCurrentIndex(0)

    # ------------------------------------------------------------ 日志

    def append_event(self, text: str, severity: str = "info") -> None:
        """往共享日志写一条。severity ∈ {info, warn, bad}。"""
        color = {"info": theme.TEXT_DIM, "warn": theme.GRADE_COLORS["fair"],
                 "bad": theme.GRADE_COLORS["bad"]}.get(severity, theme.TEXT_DIM)
        ts = time.strftime("%H:%M:%S", time.localtime())
        self.txt_events.append(
            f'<span style="color:{theme.TEXT_FAINT}">{ts}</span> '
            f'<span style="color:{color}">{text}</span>'
        )

    def clear_history(self) -> int:
        """清空延迟样本与事件日志。返回清掉的目标数（额度历史另走 _clear_campus）。

        必须**连磁盘上那份一起删**：退出时会自动保存，不清文件的话重启后
        旧数据原样回来，用户会以为"清空"这个按钮坏了。
        """
        count = self.latency.clear_history()
        latency_history.remove()
        self.txt_events.clear()
        return count

    # ------------------------------------------------------------ 设置

    def open_settings(self) -> None:
        self._open_settings()

    def _open_settings(self) -> None:
        # 必须非模态：exec() 是应用级模态，Qt 会拦掉应用内其他所有窗口的鼠标
        # 输入 —— 悬浮窗是独立顶层窗口，设置一开它一个事件都收不到，拖不动
        # 也点不了右键（用户实测踩中）。而且「改动即时生效」的工作流本来就
        # 是一边开着设置、一边盯着悬浮窗核对，模态根本没法用。
        if self.settings_dlg is not None and self.settings_dlg.isVisible():
            self.settings_dlg.raise_()
            self.settings_dlg.activateWindow()
            return
        dlg = SettingsDialog(
            self.cfg,
            on_clear_latency=self.clear_history,
            on_clear_campus=self._clear_campus,
            on_open_devices=self.openDevicesRequested.emit,
            on_test_notify=self.testNotifyRequested.emit,
            session_account=self.session_account(),
            parent=self,
        )
        # 改动即时生效：对话框开着，改一下就应用一下 —— 不用等它关掉。
        # 参数是 set[str]（哪些分组变了），和关闭时那条路径走的是同一个处理。
        dlg.settingsChanged.connect(self._apply_settings)
        # 持有引用：悬浮窗从别的入口（按钮/托盘）显隐时，对话框里的
        # 「显示悬浮窗」要被同步 —— 不然它显示的是假状态。
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.settings_dlg = dlg
        dlg.finished.connect(self._settings_closed)
        dlg.show()

    def _settings_closed(self, *_args) -> None:
        self.settings_dlg = None

    def _clear_campus(self) -> int:
        store = self.campus.store
        if store is None:
            return 0
        return store.clear_usage()

    def _apply_settings(self, changed: set[str]) -> None:
        """把改过的配置重新吸收进两个页面，并把"哪些组变了"转给 app 层。

        app 层要据此重启额度轮询、设备定时器等；页面只管把自己那张脸刷新。
        """
        if "chart_window" in changed:
            self.latency.sync_chart_window()
        if "overlay" in changed:
            if self.overlay is not None:
                self.overlay.apply_settings()
            want = bool(self.cfg.overlay_cfg.get("visible", True))
            if self.btn_overlay.isChecked() != want:
                self.btn_overlay.setChecked(want)      # 走信号，让 app 层统一显隐
        if "campus" in changed or "probe" in changed:
            self.campus.apply_settings()
        if "probe" in changed or "chart_window" in changed:
            self.latency.apply_after_settings()

        if not changed:
            # 即时生效之后，会有大量"碰了一下但又改回原值"的空变更。
            # 不记日志 —— 否则日志框会被"设置已更新"刷屏。
            # 而真有改动时**必须**记一条：用户点完一个开关，得能立刻看到
            # "它生效了"，不然只能凭感觉猜。
            return

        names = {"probe": "探测参数", "chart_window": "图表范围",
                 "overlay": "悬浮窗", "campus": "校园网额度",
                 "notify": "提醒", "device": "设备监控",
                 "startup": "启动与数据", "autostart": "开机自启动"}
        order = ["probe", "chart_window", "overlay", "campus", "notify",
                 "device", "startup", "autostart"]
        picked = [names[k] for k in order if k in changed]
        self.append_event("设置已更新（即时生效）：" + "、".join(picked))

        self.settingsChanged.emit(set(changed))

    # ------------------------------------------------------------ 对外

    def session_account(self) -> str:
        """当前在线会话账号（设备管理用它做"自动跟随"）。"""
        store = getattr(self.campus, "store", None)
        if store is None:
            return ""
        last = store.latest()
        if last and last.online and last.account:
            return last.account
        return ""

    def overlay_rows(self, window: float = 60.0) -> list[dict]:
        return self.latency.overlay_rows(window)

    def focus_target(self, tid: str) -> None:
        self.show_latency()
        self.latency.focus_target(tid)

    def pause(self, on: bool) -> None:
        self.latency.pause(on)

    def is_paused(self) -> bool:
        return self.latency.is_paused()

    def show_and_raise(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def apply_settings(self) -> None:
        """外部（app 层）改过配置后，让两页重新吸收一次。"""
        self.campus.apply_settings()
        self.latency.apply_after_settings()
        if self.overlay is not None:
            self.overlay.apply_settings()

    # ------------------------------------------------------------ 收尾

    def save_geometry(self) -> None:
        self.cfg.settings["window"] = {"w": self.width(), "h": self.height()}

    def closeEvent(self, event) -> None:  # noqa: N802
        """按「关闭窗口时退到托盘」配置分流。

        这个复选框曾经是个摆设：closeEvent 无条件 ignore+hide，勾不勾
        都是隐藏 —— 设置页里唯一"点了没反应"的开关就是它（设置即时生效
        那轮抓出来的）。False（不退托盘）时走真退出：accept 掉关闭事件并
        发 quitRequested，让 app 层的 quit() 统一做收尾（停采集、**存延迟
        历史**、关库）—— 那条链和托盘菜单「退出」是同一条，不许出现
        "菜单退出会保存、点关闭退出不保存"两条路径。
        """
        self.save_geometry()
        self.cfg.save()
        if bool(self.cfg.settings.get("close_to_tray", True)):
            event.ignore()
            self.hide()
            self.hiddenToTray.emit()
            return
        event.accept()
        self.quitRequested.emit()
