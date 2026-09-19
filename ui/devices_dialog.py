"""设备管理窗口：当前账号在线的终端列表 + 重命名 / 信任 / 强制下线。

账号是选填的：没填就自动跟随当前认证会话账号（campus.device_account）。
接口查询走线程池，UI 不阻塞；用请求序号丢弃过期响应（先发的慢请求后返回
会覆盖新数据，这是实测踩过的竞态）。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.campus.api import EPortalAPI
from core.campus.models import Device

from . import theme
from .widgets import hint

COLUMNS = ["名称", "IP 地址", "MAC", "在线时长", "本次下行", ""]


class _Signals(QObject):
    finished = Signal(object)


class _Task(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as e:       # 兜底：绝不让线程里的异常崩掉程序
            result = e
        self.signals.finished.emit(result)


class DevicesDialog(QDialog):
    """设备列表窗口。"""

    def __init__(self, cfg, store, notifier=None, local_ip: str = "",
                 session_account: str = "", parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.store = store
        self.notifier = notifier
        self.local_ip = local_ip
        self.session_account = session_account

        self._devices: list[Device] = []
        self._seq = 0
        self._busy = False
        self._pending = False

        self.setWindowTitle("设备管理 · 校园网在线终端")
        self.resize(760, 460)
        self.setStyleSheet(theme.qss())

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("当前在线终端")
        title.setObjectName("SectionTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.lb_status = QLabel("正在查询…")
        self.lb_status.setObjectName("Sub")
        head.addWidget(self.lb_status)

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh)
        head.addWidget(self.btn_refresh)
        lay.addLayout(head)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3, 4):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.doubleClicked.connect(lambda _i: self._rename_selected())
        lay.addWidget(self.table, 1)

        btns = QHBoxLayout()
        self.btn_rename = QPushButton("重命名")
        self.btn_rename.setToolTip("给这台设备起个好认的名字（只存在本地）")
        self.btn_rename.clicked.connect(self._rename_selected)
        btns.addWidget(self.btn_rename)

        self.btn_trust = QPushButton("标记为可信")
        self.btn_trust.setToolTip("信任后不再把它当成陌生设备提醒")
        self.btn_trust.clicked.connect(self._toggle_trust)
        btns.addWidget(self.btn_trust)

        btns.addStretch(1)
        self.btn_kick = QPushButton("强制下线")
        self.btn_kick.setObjectName("Danger")
        self.btn_kick.setToolTip("把选中的终端踢下线（注意：可能是你自己的设备）")
        self.btn_kick.clicked.connect(self._kick_selected)
        btns.addWidget(self.btn_kick)
        lay.addLayout(btns)

        acct = self.cfg.campus.device_account(self.session_account)
        lay.addWidget(hint(f"查询账号：{acct or '（无）'}"))

        self._timer = QTimer(self)
        self._timer.setInterval(max(30, self.cfg.campus.device_interval) * 1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    # ------------------------------------------------------------ 查询

    def refresh(self) -> None:
        if self._busy:
            self._pending = True
            return
        account = self.cfg.campus.device_account(self.session_account)
        if not account:
            self.lb_status.setText("未配置账号，且当前没有在线会话 —— 无法查询")
            self._fill([])
            return

        self._busy = True
        self._seq += 1
        seq = self._seq
        campus = self.cfg.campus
        api = EPortalAPI(campus.eportal_host, campus.eportal_port, campus.timeout)

        task = _Task(lambda: api.find_devices(account, self.local_ip))
        task.signals.finished.connect(lambda result, s=seq: self._on_done(result, s))
        # 必须持有 runnable 的 Python 引用：QThreadPool.start 不转移所有权，
        # 没有这个引用时 task（连带 signals）会在 fn 执行后被 GC，
        # finished 信号 emit 到一个已销毁的 QObject 上 —— **静默丢失**。
        # 实测症状：fn 真的跑了，但状态栏永远停在「正在查询…」。
        self._task = task
        QThreadPool.globalInstance().start(task)

    def _on_done(self, result, seq: int) -> None:
        if seq != self._seq:
            return                       # 过期响应：丢弃
        self._busy = False
        if isinstance(result, Exception):
            self.lb_status.setText(f"查询失败：{result}")
            self._fill([])
        else:
            self._fill(result)
            self.lb_status.setText(f"共 {len(result)} 台在线")
        if self._pending:
            self._pending = False
            self.refresh()

    def _fill(self, devices: list[Device]) -> None:
        aliases = self.store.aliases()
        for d in devices:
            info = aliases.get((d.mac or "").upper().replace(":", "")) or \
                aliases.get((d.mac or "").upper())
            if info:
                d.alias = info.get("alias") or ""
                d.trusted = bool(info.get("trusted"))
            d.is_local = bool(self.local_ip) and d.ip == self.local_ip

        self._devices = devices
        self.table.setRowCount(len(devices))
        for row, d in enumerate(devices):
            name = d.display_name
            if d.is_local:
                name += "  ← 本机"
            items = [
                name,
                d.ip,
                d.mac_display,
                d.time_long_text,
                f"{d.downlink_gb:.2f} GB",
                "已信任" if d.trusted else "",
            ]
            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                if col == 2:
                    item.setToolTip(d.mac_display)
                self.table.setItem(row, col, item)

    # ------------------------------------------------------------ 操作

    def _selected(self) -> Device | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        idx = rows[0].row()
        if 0 <= idx < len(self._devices):
            return self._devices[idx]
        return None

    def _rename_selected(self) -> None:
        d = self._selected()
        if d is None:
            QMessageBox.information(self, "先选一台设备", "请在上面的列表里点一行。")
            return
        text, ok = QInputDialog.getText(self, "重命名设备", "名称：", text=d.alias or "")
        if not ok:
            return
        self.store.set_alias(d.mac, text.strip(), d.trusted)
        self._fill(self._devices)

    def _toggle_trust(self) -> None:
        d = self._selected()
        if d is None:
            QMessageBox.information(self, "先选一台设备", "请在上面的列表里点一行。")
            return
        self.store.set_alias(d.mac, d.alias, not d.trusted)
        self._fill(self._devices)

    def _kick_selected(self) -> None:
        d = self._selected()
        if d is None:
            QMessageBox.information(self, "先选一台设备", "请在上面的列表里点一行。")
            return
        if QMessageBox.question(
            self, "强制下线",
            f"确定把「{d.display_name}」（{d.ip}）踢下线？\n"
            "如果那是你自己的设备，网络会断一下，需要重新认证。",
        ) != QMessageBox.StandardButton.Yes:
            return
        account = self.cfg.campus.device_account(self.session_account)
        campus = self.cfg.campus
        api = EPortalAPI(campus.eportal_host, campus.eportal_port, campus.timeout)

        self.lb_status.setText("正在下线…")

        def work():
            try:
                api.offline_device(account, d.ip)
                return True
            except Exception as e:
                return e

        task = _Task(work)
        task.signals.finished.connect(self._on_kick)
        QThreadPool.globalInstance().start(task)

    def _on_kick(self, result) -> None:
        if isinstance(result, Exception):
            self.lb_status.setText(f"下线失败：{result}")
        else:
            self.lb_status.setText("已下线，正在重新查询…")
        QTimer.singleShot(1200, self.refresh)
