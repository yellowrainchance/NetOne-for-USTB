"""编辑/新增监测目标的对话框，以及"从当前连接里挑服务器"的入口。"""
from __future__ import annotations

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.config import Target

from . import theme
from .widgets import dot_pixmap

# 常用目标模板：(名称, 主机, 端口, 探测方式)。网站类统一 TCP 443（握手延迟
# 最接近真实访问体验）；纯 IP 的公共 DNS 用 ICMP。
TARGET_PRESETS: list[tuple[str, str, int, str]] = [
    ("北科官网", "www.ustb.edu.cn", 443, "tcp"),
    ("GitHub", "github.com", 443, "tcp"),
    ("谷歌", "www.google.com", 443, "tcp"),
    ("腾讯", "www.qq.com", 443, "tcp"),
    ("百度", "www.baidu.com", 443, "tcp"),
    ("哔哩哔哩", "www.bilibili.com", 443, "tcp"),
    ("知乎", "www.zhihu.com", 443, "tcp"),
    ("淘宝", "www.taobao.com", 443, "tcp"),
    ("网易", "www.163.com", 443, "tcp"),
    ("微软官网", "www.microsoft.com", 443, "tcp"),
    ("Steam 商店", "store.steampowered.com", 443, "tcp"),
    ("YouTube", "www.youtube.com", 443, "tcp"),
    ("Cloudflare DNS", "1.1.1.1", 0, "icmp"),
    ("阿里公共 DNS", "223.5.5.5", 0, "icmp"),
]


class TargetDialog(QDialog):
    """原地修改传入的 Target 对象（不新建），这样编辑时不会重置历史样本。"""

    def __init__(self, target: Target | None, colors: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑监测目标" if target else "添加监测目标")
        self.setMinimumWidth(420)
        self._target = target or Target(color=colors[0])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)
        form = QFormLayout()
        form.setSpacing(10)

        # 常用网站模板：选中即填充下方字段（仍可手改）
        self.cb_preset = QComboBox()
        self.cb_preset.addItem("从模板选择…", None)
        for name, host, port, kind in TARGET_PRESETS:
            self.cb_preset.addItem(f"{name}（{host}）", (name, host, port, kind))
        self.cb_preset.currentIndexChanged.connect(self._apply_preset)
        form.addRow("模板", self.cb_preset)

        self.ed_name = QLineEdit(self._target.name)
        self.ed_name.setPlaceholderText("例如：EA 官网 / 游戏服务器")

        self.ed_host = QLineEdit(self._target.host)
        self.ed_host.setPlaceholderText("域名或 IP，例如 www.ea.com 或 8.8.8.8")

        host_row = QWidget()
        host_lay = QHBoxLayout(host_row)
        host_lay.setContentsMargins(0, 0, 0, 0)
        host_lay.setSpacing(6)
        host_lay.addWidget(self.ed_host, 1)
        self.btn_pick = QPushButton("从当前连接选取…")
        self.btn_pick.setToolTip(
            "扫描本机正在进行的网络连接，直接挑游戏实际连的那台服务器\n"
            "（先把游戏开起来再点这个）"
        )
        self.btn_pick.clicked.connect(self._pick_connection)
        host_lay.addWidget(self.btn_pick)

        self.cb_kind = QComboBox()
        self.cb_kind.addItem("TCP 握手（推荐，测网站/游戏服务器）", "tcp")
        self.cb_kind.addItem("ICMP Ping（测纯网络延迟）", "icmp")
        self.cb_kind.setCurrentIndex(0 if self._target.kind == "tcp" else 1)

        self.sp_port = QSpinBox()
        self.sp_port.setRange(1, 65535)
        self.sp_port.setValue(self._target.port or 443)

        self.sp_interval = QDoubleSpinBox()
        self.sp_interval.setRange(0.2, 30.0)
        self.sp_interval.setSingleStep(0.5)
        self.sp_interval.setDecimals(1)
        self.sp_interval.setSuffix(" 秒")
        self.sp_interval.setValue(self._target.interval or 1.0)

        self.sp_timeout = QSpinBox()
        self.sp_timeout.setRange(200, 10000)
        self.sp_timeout.setSingleStep(100)
        self.sp_timeout.setSuffix(" ms")
        self.sp_timeout.setValue(self._target.timeout_ms or 1500)

        self.cb_color = QComboBox()
        for c in colors:
            self.cb_color.addItem(QIcon(dot_pixmap(c, 14)), c)
        idx = colors.index(self._target.color) if self._target.color in colors else 0
        self.cb_color.setCurrentIndex(idx)

        form.addRow("名称", self.ed_name)
        form.addRow("地址", host_row)
        form.addRow("探测方式", self.cb_kind)
        form.addRow("端口", self.sp_port)
        form.addRow("探测间隔", self.sp_interval)
        form.addRow("超时", self.sp_timeout)
        form.addRow("曲线颜色", self.cb_color)
        layout.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        layout.addStretch(1)

        self.cb_kind.currentIndexChanged.connect(self._sync_port_state)
        self._sync_port_state()
        self.setStyleSheet(theme.qss())

    def _apply_preset(self) -> None:
        """模板选中即填充：名称/地址/方式/端口都给好，用户仍可手改。"""
        data = self.cb_preset.currentData()
        if not data:
            return
        name, host, port, kind = data
        self.ed_name.setText(name)
        self.ed_host.setText(host)
        self.cb_kind.setCurrentIndex(0 if kind == "tcp" else 1)
        if kind == "tcp":
            self.sp_port.setValue(port)
        self._sync_port_state()

    def _sync_port_state(self) -> None:
        self.sp_port.setEnabled(self.cb_kind.currentData() == "tcp")

    def _pick_connection(self) -> None:
        from ui.conn_picker import ConnPicker

        dlg = ConnPicker(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        picked = dlg.result_target()
        if not picked:
            return
        name, host, port, kind = picked
        if not self.ed_name.text().strip():
            self.ed_name.setText(name)
        self.ed_host.setText(host)
        self.cb_kind.setCurrentIndex(0 if kind == "tcp" else 1)
        if kind == "tcp":
            self.sp_port.setValue(port)
        self._sync_port_state()

    def _accept(self) -> None:
        host = self.ed_host.text().strip()
        if not host:
            QMessageBox.warning(self, "缺少地址", "请填写要监测的域名或 IP。")
            return
        host = host.replace("http://", "").replace("https://", "").split("/")[0]
        if ":" in host and self.cb_kind.currentData() == "tcp":
            host, _, port = host.partition(":")
            if port.isdigit():
                self.sp_port.setValue(int(port))
        self._target.name = self.ed_name.text().strip() or host
        self._target.host = host
        self._target.kind = self.cb_kind.currentData()
        self._target.port = self.sp_port.value()
        self._target.interval = self.sp_interval.value()
        self._target.timeout_ms = self.sp_timeout.value()
        self._target.color = self.cb_color.itemText(self.cb_color.currentIndex())
        self.accept()

    def result_target(self) -> Target:
        return self._target
