"""系统托盘：额度环形图标 + 全局面板菜单。

合并自两代程序：环形图标和"剩余 X GB"的状态行来自 NetQuota 的托盘，
菜单结构与"暂停监测"来自 NetPing 的托盘。合并后用同一个图标承载两件事：

  - 图标本身 = 本月剩余额度的圆环（额度是最需要"瞟一眼"的数字）
  - tooltip   = 额度明细 + 当前网络状态（两行都给全，不用点开）

托盘只是一个"遥控器"：它不改任何业务状态，所有动作都通过信号交给 app 层，
避免第二处写入配置导致两边不一致。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ui.ring import make_tray_icon


class TrayController(QObject):
    """托盘控制器。程序常驻入口，也是关窗后的唯一入口。"""

    openMainRequested = Signal()
    refreshQuotaRequested = Signal()
    pauseToggled = Signal(bool)
    overlayToggled = Signal(bool)
    openDevicesRequested = Signal()
    openSettingsRequested = Signal()
    quitRequested = Signal()

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._icon_key: tuple[int, bool] | None = None
        self._quota_text = "正在获取…"
        self._net_text = ""
        self._tip_shown = False

        self.tray = QSystemTrayIcon(self)
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        if self.available:
            self.tray.setIcon(make_tray_icon(100.0, online=False))
            self.tray.setToolTip("NetOne 启动中…")
            self.tray.activated.connect(self._on_activated)

        self._build_menu()
        if self.available:
            self.tray.setContextMenu(self.menu)
            self.tray.show()

    # ---------------------------------------------------------------- 菜单

    def _build_menu(self) -> None:
        menu = QMenu()
        menu.setStyleSheet("QMenu { padding: 4px; }")

        # 第一行是纯状态展示，点了没反应，所以置灰
        self.act_status = menu.addAction("正在获取额度…")
        self.act_status.setEnabled(False)
        menu.addSeparator()

        act_main = menu.addAction("打开主面板")
        act_main.triggered.connect(self.openMainRequested.emit)

        act_refresh = menu.addAction("立即刷新额度")
        act_refresh.triggered.connect(self.refreshQuotaRequested.emit)

        self.act_pause = menu.addAction("暂停延迟监测")
        self.act_pause.setCheckable(True)
        self.act_pause.toggled.connect(self.pauseToggled.emit)

        menu.addSeparator()

        self.act_overlay = menu.addAction("桌面悬浮窗")
        self.act_overlay.setCheckable(True)
        self.act_overlay.setChecked(bool(self.cfg.overlay_cfg.get("visible", True)))
        self.act_overlay.toggled.connect(self.overlayToggled.emit)

        self.act_devices = menu.addAction("设备管理…")
        self.act_devices.triggered.connect(self.openDevicesRequested.emit)

        act_settings = menu.addAction("设置…")
        act_settings.triggered.connect(self.openSettingsRequested.emit)

        menu.addSeparator()

        act_quit = menu.addAction("退出 NetOne")
        act_quit.triggered.connect(self.quitRequested.emit)

        self.menu = menu

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.openMainRequested.emit()

    # ---------------------------------------------------------------- 同步

    def set_overlay_checked(self, on: bool) -> None:
        """外部改了悬浮窗显隐后回填勾选（拦信号，避免来回触发）。"""
        if self.act_overlay.isChecked() == on:
            return
        self.act_overlay.blockSignals(True)
        self.act_overlay.setChecked(on)
        self.act_overlay.blockSignals(False)

    def set_paused(self, on: bool) -> None:
        if self.act_pause.isChecked() == on:
            return
        self.act_pause.blockSignals(True)
        self.act_pause.setChecked(on)
        self.act_pause.blockSignals(False)

    def set_device_hint(self, count: int, limit: int = 0) -> None:
        if limit > 0 and count > limit:
            self.act_devices.setText(f"设备管理… 在线 {count} 台（超上限 {limit}）")
        elif count >= 0:
            self.act_devices.setText(f"设备管理… 在线 {count} 台")

    # ---------------------------------------------------------------- 更新

    def update_quota(self, info: dict | None) -> None:
        """info 与悬浮窗的 set_quota 同形：{left_gb, pct_left, online, error, ...}"""
        info = info or {}
        online = bool(info.get("online"))
        if not info:
            self._quota_text = "正在获取…"
        elif not online:
            err = (info.get("error") or "未连接校园网").strip()
            self._quota_text = f"离线 · {err[:28]}"
        else:
            left = float(info.get("left_gb") or 0.0)
            pct_left = float(info.get("pct_left") or 0.0)
            used = float(info.get("used_gb") or 0.0)
            total = float(info.get("total_gb") or 0.0)
            price = float(info.get("price") or 0.0)
            if left < 0:
                self._quota_text = f"已超额 {-left:.2f} GB · ¥{price * -left:.2f}"
            else:
                self._quota_text = f"剩余 {left:.2f} GB · 已用 {used:.1f}/{total:.0f} GB"
            if pct_left:
                self._quota_text += f"（{pct_left:.0f}%）"

        pct = float(info.get("pct_left") or 0.0)
        key = (int(round(pct)), online)
        if key != self._icon_key:
            self._icon_key = key
            if self.available:
                self.tray.setIcon(make_tray_icon(pct, online=online))

        self.act_status.setText("NetOne · " + self._quota_text)
        self._refresh_tip()

    def update_network(self, text: str) -> None:
        """第二行 tooltip：当前网络状态（来自延迟页的状态播报）。"""
        text = (text or "").strip()
        if text == self._net_text:
            return
        self._net_text = text
        self._refresh_tip()

    def _refresh_tip(self) -> None:
        if not self.available:
            return
        lines = ["NetOne", self._quota_text]
        if self._net_text:
            lines.append(f"网络：{self._net_text}")
        self.tray.setToolTip("\n".join(lines))

    # ---------------------------------------------------------------- 通知

    def notify(self, title: str, message: str, warn: bool = True) -> None:
        """额度告警等的兜底出口；没有托盘时静默忽略（不能因为通知失败影响主流程）。"""
        if not self.available:
            return
        icon = (QSystemTrayIcon.MessageIcon.Warning if warn
                else QSystemTrayIcon.MessageIcon.Information)
        self.tray.showMessage(title, message, icon, 5000)

    def tip_once(self, title: str, message: str) -> bool:
        """只弹一次的气泡（关窗收进托盘时提示用）。返回本次是否真的弹了。"""
        if self._tip_shown:
            return False
        self._tip_shown = True
        self.notify(title, message, warn=False)
        return True

    def hide(self) -> None:
        if self.available:
            self.tray.hide()
