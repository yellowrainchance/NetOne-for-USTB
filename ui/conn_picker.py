"""从当前活跃连接里挑一个作为监测目标。

免得用户为了"测游戏服务器"还得自己去查 IP。
netstat/tasklist 要几百毫秒到两秒，所以放后台线程跑，主线程轮询取结果。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from core.net.conns import Conn, grouped, list_connections

from ui import theme
from ui.widgets import WrapLabel


class ConnPicker(QDialog):
    """选中后返回 (name, host, port, kind)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("从当前连接中选择")
        self.resize(720, 520)
        self._result: tuple[str, str, int, str] | None = None
        self._conns: list[Conn] = []
        self._pending: list[Conn] | None = None
        self._poll: QTimer | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(9)

        bar = QHBoxLayout()
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("过滤：进程名或 IP，例如 ea / 1.2.3.4")
        self.ed_filter.textChanged.connect(self._fill_tree)
        bar.addWidget(self.ed_filter, 1)

        self.cb_noise = QCheckBox("隐藏浏览器/系统进程")
        self.cb_noise.setChecked(True)
        self.cb_noise.toggled.connect(self._reload)
        bar.addWidget(self.cb_noise)

        self.btn_refresh = QPushButton("重新读取")
        self.btn_refresh.clicked.connect(self._reload)
        bar.addWidget(self.btn_refresh)
        lay.addLayout(bar)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["进程 / 远端地址", "端口", "协议", "网络"])
        self.tree.setColumnWidth(0, 380)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.itemSelectionChanged.connect(self._on_select)
        self.tree.itemDoubleClicked.connect(self._accept_selected)
        lay.addWidget(self.tree, 1)

        self.lb_sel = WrapLabel("")
        self.lb_sel.setObjectName("Sub")
        lay.addWidget(self.lb_sel)

        btns = QDialogButtonBox()
        self.btn_add = btns.addButton("设为监测目标", QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_add.setObjectName("Primary")
        self.btn_add.setEnabled(False)
        self.btn_add.clicked.connect(self._accept_selected)
        cancel = btns.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        cancel.clicked.connect(self.reject)
        lay.addWidget(btns)

        self._reload()

    # ------------------------------------------------------------ 取数

    def _reload(self) -> None:
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("读取中…")
        self._pending = None

        no_noise = self.cb_noise.isChecked()

        def work() -> None:
            # 子线程只写变量，主线程轮询取走
            self._pending = list_connections(include_noise=not no_noise)

        threading.Thread(target=work, daemon=True).start()
        if self._poll is None:
            self._poll = QTimer(self)
            self._poll.setInterval(150)
            self._poll.timeout.connect(self._check)
        self._poll.start()

    def _check(self) -> None:
        if self._pending is None:
            return
        self._conns = self._pending
        self._pending = None
        if self._poll is not None:
            self._poll.stop()
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText("重新读取")
        self._fill_tree()

    # ------------------------------------------------------------ 展示

    def _fill_tree(self) -> None:
        needle = self.ed_filter.text().strip().lower()
        self.tree.clear()
        count = 0
        for proc, items in grouped(self._conns):
            if needle:
                items = [c for c in items if needle in proc.lower() or needle in c.remote_ip]
                if not items:
                    continue
            parent = QTreeWidgetItem(self.tree)
            parent.setText(0, f"{proc}    （{len(items)} 条）")
            parent.setText(2, items[0].proto if items else "")
            f = QFont(parent.font(0))
            f.setBold(True)
            parent.setFont(0, f)
            for c in items:
                child = QTreeWidgetItem(parent)
                child.setText(0, c.remote_ip)
                child.setText(1, str(c.remote_port))
                child.setText(2, c.proto)
                child.setText(3, "内网" if c.is_lan else "公网")
                child.setData(0, Qt.ItemDataRole.UserRole, (proc, c.remote_ip, c.remote_port, c.proto))
                if c.is_lan:
                    child.setForeground(0, QColor(theme.TEXT_FAINT))
                count += 1
            parent.setExpanded(True)
        if count == 0:
            empty = QTreeWidgetItem(self.tree)
            empty.setText(0, "没有匹配的活跃连接（游戏没开？或者都被过滤了）")
            empty.setForeground(0, QColor(theme.TEXT_FAINT))
        self.lb_sel.setText(
            f"共 {count} 条连接。" + ("选中一条即可设为监测目标。" if count else "")
        )

    def _current(self) -> tuple[str, str, int, str] | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            # 选中的是父节点（进程）：取组内第一条
            child = item.child(0)
            if child is None:
                return None
            data = child.data(0, Qt.ItemDataRole.UserRole)
        return data

    def _on_select(self) -> None:
        picked = self._current()
        self.btn_add.setEnabled(picked is not None)
        if picked is None:
            return
        proc, ip, port, proto = picked
        stem = proc.rsplit(".", 1)[0]
        if proto == "UDP":
            self.lb_sel.setText(
                f"将添加：{stem} → {ip}\n"
                f"这是 UDP 连接，没法用 TCP 握手测，会用 ICMP Ping 监测。"
            )
        else:
            self.lb_sel.setText(f"将添加：{stem} → {ip}:{port}（TCP）")

    def _accept_selected(self) -> None:
        picked = self._current()
        if picked is None:
            return
        proc, ip, port, proto = picked
        stem = proc.rsplit(".", 1)[0] or proc
        kind = "icmp" if proto == "UDP" else "tcp"
        self._result = (stem, ip, port, kind)
        self.accept()

    def result_target(self) -> tuple[str, str, int, str] | None:
        return self._result
