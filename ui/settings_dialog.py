"""统一设置面板：左侧分类 + 右侧分页。

## 为什么长这样（修的是真实 bug）

合并前的设置面板把三组设置直接堆在一个 QVBoxLayout 里，结果是：

1. 对话框总高 719px，小屏/高缩放下底部按钮会跑到屏幕外；
2. **换行提示文字被布局压扁**——「探测」那组的说明需要 70px 但只分到 42px，
   最后一行"1000ms，出境线路本身就要几百毫秒。"直接被裁掉，用户看到的是
   半句没说完的话。

两处都改掉了：每页内容放进 QScrollArea（放不下就滚动，绝不裁），
对话框高度上限压到屏幕可用高度；所有说明文字一律用 WrapLabel
（它会把自己的 minimumHeight 同步成 heightForWidth，布局压不动它）。

## 改动即时生效（2026-09-19 按用户要求改）

原来只有点「保存」才落盘，用户要求「一点击就立刻生效」。改动之后：

- 复选框、下拉框：**立即**应用 —— 点一下就是一下；
- 数字框、文本框：等 400ms 没新输入再应用（输「1200」的途中会依次变成
  1、12、120，每一下都写盘既浪费又会把探测间隔瞬间设成 1 秒）；
- 「保存/取消」这一对**必须撤掉**：改动已经生效了，一个还能"取消"的按钮
  就是在骗人。换成一个「关闭」，旁边写明"改动即时生效"。
  把「取消」留着的唯一方式是实现真回滚，那要再写一套「从配置反填控件」，
  和 commit() 一一对应 —— 两份镜像代码必然走偏，不做。

校验不通过时不落库、也不改回默认值，而是在控件上标红：静默把用户输的东西
换成默认值，是"界面在骗人"的另一种写法。
"""
from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core import autostart
from core.config import (
    CHART_WINDOWS,
    OVERLAY_W_MAX,
    OVERLAY_W_MIN,
    QUOTA_INTERVALS,
    Config,
    data_dir,
)

from . import theme
from .widgets import WrapLabel

PAGES = ["探测", "悬浮窗", "校园网", "提醒", "设备", "启动与数据"]

# 整行控件（复选框、说明、按钮）的左缩进。
#
# 实测：分隔线右缘在 x=153，复选框指示器在 x=167 —— 只差 14px，看着就是贴在
# 边框上。这一族行都是 `f.addRow(单个控件)`（跨两列）加的，而表单左边距只有
# 4px，所以它们比右对齐的标签列多探出去一大截。加一点缩进把它们推离边框。
CHECK_INDENT = 22

# 数字框/文本框的防抖窗口（毫秒）
LIVE_DEBOUNCE_MS = 400


def _scrolled(widget: QWidget) -> QScrollArea:
    """把一页内容塞进滚动区。

    设置项只会越来越多（两个项目合起来已经有 6 页），而屏幕高度不会变。
    定死高度 + 内容裁掉是不行的（就是原来那个 bug），所以让内容滚动：
    放得下时滚动条不出现，放不下时出现，任何分辨率都能摸到最后一页设置。
    """
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    # 竖直滚动条按需出现（默认 AsNeeded，这里写明是为了让人一眼看到意图）
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setWidget(widget)
    return area


def _indent(widget: QWidget, left: int = CHECK_INDENT) -> QWidget:
    """给整行控件加一点左缩进，让它别贴着内容区左边框。

    为什么不直接用 `f.addRow("", widget)`（塞进字段列）：那会把控件推到
    数字框那一列（实测 x≈257），比"稍微右移一点"多得多，而且复选框一列
    比标签还靠右会显得莫名其妙。所以用一个只带左边距的容器。
    """
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(left, 0, 0, 0)
    row.setSpacing(0)
    row.addWidget(widget)
    row.addStretch(1)
    return box


class SettingsDialog(QDialog):
    """集中设置。改动即时落盘，并**通过 settingsChanged 通知外面立刻应用**。

    保留 `commit()` 作为唯一的"读界面 → 写配置"入口：即时应用和关闭前收尾
    走的是同一条路，不会出现"手动保存和自动保存结果不一样"。
    """

    # 参数是 set[str]（被改动的分组名）。用 object 是因为 set 不是 Qt 内置类型
    settingsChanged = Signal(object)

    def __init__(
        self,
        cfg: Config,
        on_clear_latency: Callable[[], int] | None = None,
        on_clear_campus: Callable[[], int] | None = None,
        on_open_devices: Callable[[], None] | None = None,
        on_test_notify: Callable[[], None] | None = None,
        session_account: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.cfg = cfg
        self.campus = cfg.campus
        self._on_clear_latency = on_clear_latency
        self._on_clear_campus = on_clear_campus
        self._on_open_devices = on_open_devices
        self._on_test_notify = on_test_notify
        self.session_account = (session_account or "").strip()
        self._w: dict[str, QWidget] = {}
        self.changed: set[str] = set()
        # 正在把配置往控件上写、或者正在应用改动时的重入闸门。
        # 没有它的话，commit() 里那句"自启动失败就把开关拨回去"会再触发一次
        # toggled → 又一次 commit，来回弹。
        self._busy = False

        self.setWindowTitle("NetOne 设置")
        self.setStyleSheet(theme.qss())
        self.setMinimumSize(620, 440)

        # 高度上限压到屏幕可用高度：否则在高 DPI / 小屏上底部按钮会跑到屏幕外
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry().height() if screen else 800
        self.resize(760, min(640, max(440, avail - 120)))
        self.setMaximumHeight(max(440, avail - 80))

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 12)
        root.setSpacing(10)

        body = QHBoxLayout()
        body.setSpacing(10)
        self._nav = QListWidget()
        self._nav.setObjectName("Nav")
        self._nav.setFixedWidth(126)
        self._nav.addItems(PAGES)
        self._nav.setCurrentRow(0)
        body.addWidget(self._nav)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setStyleSheet(f"color:{theme.BORDER};")
        body.addWidget(line)

        self._stack = QStackedWidget()
        body.addWidget(self._stack, 1)
        root.addLayout(body, 1)

        for builder in (self._page_probe, self._page_overlay, self._page_campus,
                        self._page_notify, self._page_device, self._page_startup):
            self._stack.addWidget(_scrolled(builder()))
        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)

        # 页脚：没有「保存」——改动即时生效，只有一个「关闭」。
        foot = QHBoxLayout()
        foot.setContentsMargins(2, 0, 2, 0)
        foot.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.setObjectName("Primary")
        btn_close.clicked.connect(self.close)
        foot.addWidget(btn_close)
        root.addLayout(foot)

        self._wire_live()

    # ------------------------------------------------------------ 即时生效

    def _wire_live(self) -> None:
        """把每个控件都挂上"改了就走一遍 commit"。

        两种节奏，因为这两类控件的"一次输入"含义不同：
        - 复选框 / 下拉框：点一下就是一个完整决定 → 立即应用；
        - 数字框 / 文本框：输入途中全是中间态（输 1200 会依次变成 1、12、120）
          → 等 400ms 没有新输入再应用。
        """
        self._live = QTimer(self)
        self._live.setSingleShot(True)
        self._live.setInterval(LIVE_DEBOUNCE_MS)
        self._live.timeout.connect(self._apply_live)

        for cb in self.findChildren(QCheckBox):
            cb.toggled.connect(self._apply_now)
        for combo in self.findChildren(QComboBox):
            combo.currentIndexChanged.connect(self._apply_now)
        # QSpinBox 和 QDoubleSpinBox 是兄弟，共同基类是 QAbstractSpinBox
        for box in self.findChildren(QAbstractSpinBox):
            box.valueChanged.connect(self._apply_soon)
        for ed in self.findChildren(QLineEdit):
            # QAbstractSpinBox 内部自带一个 QLineEdit，别重复挂
            if isinstance(ed.parent(), QAbstractSpinBox):
                continue
            ed.editingFinished.connect(self._apply_soon)

    def _apply_now(self, *_args) -> None:
        """点击类控件：马上应用，顺手取消还没到点的防抖。"""
        if self._busy:
            return
        self._live.stop()
        self._apply_live()

    def _apply_soon(self, *_args) -> None:
        """输入类控件：重新计时，等用户停手。"""
        if self._busy:
            return
        self._live.start()

    def _apply_live(self) -> None:
        changed = self.commit()
        if changed:
            self.settingsChanged.emit(set(changed))

    def sync_overlay_visible(self, visible: bool) -> None:
        """外部显隐变化（主窗口按钮 / 托盘菜单）时同步「显示悬浮窗」。

        对话框开着时用户从别的入口动了悬浮窗，这里若不同步，复选框显示
        的就是假状态 —— 下一次在对话框里的操作会按假状态覆盖回去。
        blockSignals 防止同步动作又被当成"用户改动"绕回 commit。
        """
        if self.ck_ov_show.isChecked() != visible:
            self.ck_ov_show.blockSignals(True)
            self.ck_ov_show.setChecked(visible)
            self.ck_ov_show.blockSignals(False)

    def sync_overlay_width(self, width: int) -> None:
        """悬浮窗被拖宽后同步宽度输入框（防拖宽被对话框旧值写回）。"""
        if self.sp_ov_width.value() != width:
            self.sp_ov_width.blockSignals(True)
            self.sp_ov_width.setValue(width)
            self.sp_ov_width.blockSignals(False)

    # ------------------------------------------------------------ 分页

    def _page_probe(self) -> QWidget:
        w, f = self._form()

        self.ck_latency_enabled = QCheckBox("启用延迟监测（Ping 探测）")
        self.ck_latency_enabled.setChecked(bool(self.cfg.settings.get("latency_enabled")))
        self.ck_latency_enabled.setToolTip(
            "默认关闭：程序启动只做校园网检测，需要时再手动打开。\n"
            "开关会记住，重启后保持；校园网额度监测不受它影响。")
        f.addRow(_indent(self.ck_latency_enabled))

        self.sp_interval = QDoubleSpinBox()
        self.sp_interval.setRange(0.2, 30.0)
        self.sp_interval.setSingleStep(0.5)
        self.sp_interval.setDecimals(1)
        self.sp_interval.setSuffix(" 秒")
        self.sp_interval.setValue(self.cfg.interval)

        self.sp_timeout = QSpinBox()
        self.sp_timeout.setRange(200, 10000)
        self.sp_timeout.setSingleStep(100)
        self.sp_timeout.setSuffix(" ms")
        self.sp_timeout.setValue(self.cfg.timeout_ms)

        self.cb_window = QComboBox()
        for label, sec in CHART_WINDOWS:
            self.cb_window.addItem(label, sec)
        cur = self.cfg.settings.get("chart_window", 300)
        self.cb_window.setCurrentIndex(
            next((i for i, (_l, s) in enumerate(CHART_WINDOWS) if s == cur), 1)
        )

        f.addRow("探测间隔", self.sp_interval)
        f.addRow("单次超时", self.sp_timeout)
        f.addRow("延迟曲线默认范围", self.cb_window)
        return w

    def _page_overlay(self) -> QWidget:
        w, f = self._form()
        oc = self.cfg.overlay_cfg

        self.ck_ov_show = QCheckBox("显示悬浮窗")
        self.ck_ov_show.setChecked(bool(oc.get("visible", True)))
        self.ck_ov_show.setToolTip("游戏时看的就是它；关掉后仍可在主面板标题栏打开")
        f.addRow(_indent(self.ck_ov_show))

        self.ck_ov_quota = QCheckBox("显示额度区（环形进度 + 剩余 GB）")
        self.ck_ov_quota.setChecked(bool(oc.get("show_quota", True)))
        f.addRow(_indent(self.ck_ov_quota))

        self.ck_ov_latency = QCheckBox("显示延迟行（各目标实时 RTT）")
        self.ck_ov_latency.setChecked(bool(oc.get("show_latency", True)))
        f.addRow(_indent(self.ck_ov_latency))

        self.ck_ov_ping = QCheckBox("显示当前延迟（单行 Ping 摘要）")
        self.ck_ov_ping.setChecked(bool(oc.get("show_ping", True)))
        f.addRow(_indent(self.ck_ov_ping))

        # 「当前延迟」显示哪个目标：与悬浮窗右键菜单的「当前延迟来源」同源
        self.cb_ov_ping_target = QComboBox()
        self.cb_ov_ping_target.addItem("第一个启用的目标", "")
        for t in self.cfg.enabled_targets():
            self.cb_ov_ping_target.addItem(t.name, t.id)
        idx = self.cb_ov_ping_target.findData(str(oc.get("ping_tid") or ""))
        self.cb_ov_ping_target.setCurrentIndex(idx if idx >= 0 else 0)
        f.addRow("当前延迟显示", self.cb_ov_ping_target)

        self.ck_ov_used = QCheckBox("额度区显示已用流量")
        self.ck_ov_used.setChecked(bool(oc.get("show_used", True)))
        f.addRow(_indent(self.ck_ov_used))

        self.ck_ov_balance = QCheckBox("额度区显示账户余额（¥）")
        self.ck_ov_balance.setChecked(bool(oc.get("show_balance", True)))
        f.addRow(_indent(self.ck_ov_balance))

        self.ck_ov_compact = QCheckBox("紧凑模式（行更矮，占地更小）")
        self.ck_ov_compact.setChecked(bool(oc.get("compact")))
        f.addRow(_indent(self.ck_ov_compact))

        self.ck_ov_locked = QCheckBox("锁定位置和大小（防误拖）")
        self.ck_ov_locked.setChecked(bool(oc.get("locked")))
        f.addRow(_indent(self.ck_ov_locked))

        self.sp_ov_opacity = QSpinBox()
        self.sp_ov_opacity.setRange(35, 100)
        self.sp_ov_opacity.setSingleStep(5)
        self.sp_ov_opacity.setSuffix(" %")
        self.sp_ov_opacity.setValue(int(round(float(oc.get("opacity") or 0.86) * 100)))
        f.addRow("背景不透明度", self.sp_ov_opacity)

        self.sp_ov_width = QSpinBox()
        self.sp_ov_width.setRange(OVERLAY_W_MIN, OVERLAY_W_MAX)
        self.sp_ov_width.setSingleStep(20)
        self.sp_ov_width.setSuffix(" px")
        self.sp_ov_width.setValue(int(oc.get("w") or 300))
        f.addRow("宽度", self.sp_ov_width)
        return w

    def _page_campus(self) -> QWidget:
        w, f = self._form()
        c = self.campus

        self.ck_campus_on = QCheckBox("启用校园网额度监测")
        self.ck_campus_on.setChecked(bool(c.enabled))
        f.addRow(_indent(self.ck_campus_on))

        self.ed_account = QLineEdit(c.account)
        self.ed_account.setPlaceholderText("选填；留空=自动跟随当前认证会话账号")
        f.addRow("校园网账号（选填）", self.ed_account)

        if self.session_account:
            btn = QPushButton(f"填入当前会话账号：{self.session_account}")
            btn.setToolTip("把当前正在认证上网的账号填进输入框（仍需保存）；"
                           "不填也会自动跟随")
            btn.clicked.connect(lambda: self.ed_account.setText(self.session_account))
            f.addRow("", btn)

        self.cb_suffix = QComboBox()
        for value, label in (("", "默认（校园网出口）"), ("@dx", "电信 @dx"), ("@lt", "联通 @lt")):
            self.cb_suffix.addItem(label, value)
        idx = self.cb_suffix.findData(c.suffix)
        self.cb_suffix.setCurrentIndex(idx if idx >= 0 else 0)
        f.addRow("运营商后缀", self.cb_suffix)

        self.ed_portal = QLineEdit(c.portal_url)
        f.addRow("门户地址", self.ed_portal)

        self.ed_eportal_host = QLineEdit(c.eportal_host)
        f.addRow("ePortal 主机", self.ed_eportal_host)

        self.sp_eportal_port = QSpinBox()
        self.sp_eportal_port.setRange(1, 65535)
        self.sp_eportal_port.setValue(c.eportal_port)
        f.addRow("ePortal 端口", self.sp_eportal_port)

        self.sp_campus_timeout = QDoubleSpinBox()
        self.sp_campus_timeout.setRange(1.0, 30.0)
        self.sp_campus_timeout.setSingleStep(0.5)
        self.sp_campus_timeout.setDecimals(1)
        self.sp_campus_timeout.setSuffix(" 秒")
        self.sp_campus_timeout.setValue(c.timeout)
        f.addRow("查询超时", self.sp_campus_timeout)

        self.cb_quota_interval = QComboBox()
        for sec in QUOTA_INTERVALS:
            self.cb_quota_interval.addItem(f"{sec} 秒", sec)
        idx = self.cb_quota_interval.findData(c.poll_interval)
        if idx < 0:
            self.cb_quota_interval.addItem(f"{c.poll_interval} 秒", c.poll_interval)
            idx = self.cb_quota_interval.count() - 1
        self.cb_quota_interval.setCurrentIndex(idx)
        self.cb_quota_interval.setToolTip("额度变化很慢，不必查得太勤；不建议低于 30 秒")
        f.addRow("额度查询间隔", self.cb_quota_interval)

        self.sp_quota_gb = QDoubleSpinBox()
        self.sp_quota_gb.setRange(0.0, 10000.0)
        self.sp_quota_gb.setSingleStep(10.0)
        self.sp_quota_gb.setDecimals(1)
        self.sp_quota_gb.setSuffix(" GB")
        self.sp_quota_gb.setValue(c.quota_gb)
        f.addRow("每月免费额度", self.sp_quota_gb)

        self.cb_gb_base = QComboBox()
        self.cb_gb_base.addItem("1024（二进制，推荐）", 1024)
        self.cb_gb_base.addItem("1000（十进制）", 1000)
        self.cb_gb_base.setCurrentIndex(0 if c.gb_base == 1024 else 1)
        f.addRow("1 GB 等于多少 MB", self.cb_gb_base)

        self.sp_price = QDoubleSpinBox()
        self.sp_price.setRange(0.0, 100.0)
        self.sp_price.setSingleStep(0.1)
        self.sp_price.setDecimals(2)
        self.sp_price.setSuffix(" 元/GB")
        self.sp_price.setValue(c.price_per_gb)
        f.addRow("超额单价", self.sp_price)

        self.cb_quota_source = QComboBox()
        self.cb_quota_source.addItem("接口 v4（与自助服务权威账本一致，推荐）", "v4")
        self.cb_quota_source.addItem("门户首页 flow（滞后口径，偏小）", "flow")
        self.cb_quota_source.setCurrentIndex(0 if c.quota_source == "v4" else 1)
        f.addRow("额度口径", self.cb_quota_source)
        return w

    def _page_notify(self) -> QWidget:
        w, f = self._form()
        c = self.campus

        self.ck_notify = QCheckBox("启用通知")
        self.ck_notify.setChecked(bool(c.notify_enabled))
        f.addRow(_indent(self.ck_notify))

        self.ck_notify_quota = QCheckBox("剩余额度提醒")
        self.ck_notify_quota.setChecked(bool(c.notify_quota))
        f.addRow(_indent(self.ck_notify_quota))

        self.ck_notify_surge = QCheckBox("用量突增提醒")
        self.ck_notify_surge.setChecked(bool(c.notify_surge))
        f.addRow(_indent(self.ck_notify_surge))

        self.ck_notify_forecast = QCheckBox("预测超额提醒")
        self.ck_notify_forecast.setChecked(bool(c.notify_forecast))
        f.addRow(_indent(self.ck_notify_forecast))

        self.ck_notify_balance = QCheckBox("余额不足提醒")
        self.ck_notify_balance.setChecked(bool(c.notify_balance))
        f.addRow(_indent(self.ck_notify_balance))

        self.ck_notify_device = QCheckBox("设备上下线提醒")
        self.ck_notify_device.setChecked(bool(c.notify_device))
        f.addRow(_indent(self.ck_notify_device))

        self.sp_warn_left = self._dbl(c.warn_left_gb, 0.0, 1000.0, 1.0, 1)
        f.addRow("提醒阈值 剩余", self.sp_warn_left)

        self.sp_crit_left = self._dbl(c.critical_left_gb, 0.0, 1000.0, 1.0, 1)
        f.addRow("紧急阈值 剩余", self.sp_crit_left)

        self.sp_warn_balance = self._dbl(c.warn_balance_yuan, 0.0, 1000.0, 0.5, 2)
        f.addRow("余额低于", self.sp_warn_balance)

        self.sp_surge = self._dbl(c.surge_factor, 1.1, 10.0, 0.1, 1)
        f.addRow("突增倍数", self.sp_surge)

        self.ed_quiet = QLineEdit(c.quiet_hours)
        self.ed_quiet.setPlaceholderText("23:00-08:00；留空表示不启用免打扰")
        f.addRow("免打扰时段", self.ed_quiet)

        self.sp_cooldown = QSpinBox()
        self.sp_cooldown.setRange(0, 1440)
        self.sp_cooldown.setSingleStep(5)
        self.sp_cooldown.setSuffix(" 分钟")
        self.sp_cooldown.setValue(c.notify_cooldown)
        f.addRow("同类提醒冷却", self.sp_cooldown)

        btn = QPushButton("发送测试通知")
        btn.clicked.connect(lambda: self._on_test_notify() if self._on_test_notify else None)
        f.addRow("", btn)
        return w

    def _page_device(self) -> QWidget:
        w, f = self._form()
        c = self.campus

        self.ck_device = QCheckBox("启用设备监控")
        self.ck_device.setChecked(bool(c.device_check))
        f.addRow(_indent(self.ck_device))

        self.sp_device_interval = QSpinBox()
        self.sp_device_interval.setRange(60, 3600)
        self.sp_device_interval.setSingleStep(30)
        self.sp_device_interval.setSuffix(" 秒")
        self.sp_device_interval.setValue(c.device_interval)
        f.addRow("刷新间隔", self.sp_device_interval)

        self.sp_max_devices = QSpinBox()
        self.sp_max_devices.setRange(1, 20)
        self.sp_max_devices.setValue(c.max_devices)
        f.addRow("终端数上限", self.sp_max_devices)

        btn = QPushButton("打开设备管理…")
        btn.clicked.connect(lambda: self._on_open_devices() if self._on_open_devices else None)
        f.addRow("", btn)
        return w

    def _page_startup(self) -> QWidget:
        w, f = self._form()

        self.ck_autostart = QCheckBox("开机自动启动")
        self.ck_autostart.setChecked(autostart.is_enabled())
        self.ck_autostart.setToolTip(
            "登录 Windows 后自动开始监测，主面板不弹出，只留悬浮窗和托盘图标。\n"
            + autostart.describe()
        )
        f.addRow(_indent(self.ck_autostart))

        self.ck_close_tray = QCheckBox("关闭窗口时退到托盘")
        self.ck_close_tray.setChecked(bool(self.cfg.settings.get("close_to_tray", True)))
        f.addRow(_indent(self.ck_close_tray))

        self.ck_start_tray = QCheckBox("启动后只驻留托盘（不弹主面板）")
        self.ck_start_tray.setChecked(bool(self.cfg.settings.get("start_to_tray", False)))
        f.addRow(_indent(self.ck_start_tray))

        row = QHBoxLayout()
        btn_dir = QPushButton("打开配置目录")
        btn_dir.setToolTip("配置、数据库、日志都在 data/ 文件夹里，"
                           "想手工改或备份都从这里拿")
        btn_dir.clicked.connect(self._open_config_dir)
        row.addWidget(btn_dir)

        btn_clear_lat = QPushButton("清空延迟记录")
        btn_clear_lat.setToolTip("丢掉各目标已采集的延迟样本和事件日志（不删目标）")
        btn_clear_lat.clicked.connect(self._clear_latency)
        row.addWidget(btn_clear_lat)

        btn_clear_cam = QPushButton("清空额度历史")
        btn_clear_cam.setToolTip("丢掉额度快照与日汇总，统计从零开始（不删设备别名）")
        btn_clear_cam.clicked.connect(self._clear_campus)
        row.addWidget(btn_clear_cam)
        row.addStretch(1)
        f.addRow(row)

        path = WrapLabel(os.path.join(data_dir(), "config.json"))
        path.setObjectName("Faint")
        f.addRow(path)
        return w

    # ------------------------------------------------------------ 控件工厂

    def _form(self):
        w = QWidget()
        f = QFormLayout(w)
        f.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        f.setSpacing(10)
        f.setContentsMargins(4, 4, 8, 8)
        f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        return w, f

    def _dbl(self, value: float, lo: float, hi: float, step: float,
             decimals: int) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(lo, hi)
        box.setSingleStep(step)
        box.setDecimals(decimals)
        box.setValue(value)
        return box

    def _apply_quiet_hours(self, campus) -> None:
        """免打扰时段：格式不合法就**不写进配置**，并把输入框标红。

        改成立即生效之后，这条的处理方式也必须跟着变。以前是"不合法就恢复
        默认值 23:00-08:00" —— 那是静默往配置里写一个用户没输过的值，而
        输入框里还显示着他敲的东西，界面和程序对不上。现在：不合法就保留
        上一次的有效值不动，并让用户**看得见**哪里不对。
        """
        from core.notify import parse_hhmm

        text = self.ed_quiet.text().strip()
        if not text:
            self._mark_invalid(self.ed_quiet, False, "")
            campus.quiet_hours = ""
            return

        parts = text.split("-", 1)
        ok = (len(parts) == 2 and parse_hhmm(parts[0]) is not None
              and parse_hhmm(parts[1]) is not None)
        self._mark_invalid(
            self.ed_quiet, not ok,
            "格式应为 22:30-07:00 这样（24 小时制）。当前内容看不懂，"
            "所以这条设置**没有生效**，仍在用上一次的值。",
        )
        if ok:
            campus.quiet_hours = text

    @staticmethod
    def _mark_invalid(widget: QWidget, bad: bool, why: str) -> None:
        """标红 + 说明原因。

        用样式表而不是弹消息框：这是用户**打字过程中**反复发生的事，
        每敲一个字符弹一次框是灾难。
        """
        widget.setStyleSheet(
            f"border: 1px solid {theme.QUOTA_RED}; border-radius: 7px;"
            if bad else ""
        )
        widget.setToolTip(why if bad else "")

    # ------------------------------------------------------------ 动作

    def _open_config_dir(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(data_dir()))

    def _clear_latency(self) -> None:
        if self._on_clear_latency is None:
            return
        if QMessageBox.question(
            self, "清空延迟记录",
            "将丢弃所有目标已采集的延迟样本和事件日志，统计从零开始。\n"
            "磁盘上那份自动保存的历史也会一起删掉（否则重启后它又会回来）。\n"
            "目标本身会保留。确定吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        count = self._on_clear_latency()
        QMessageBox.information(
            self, "已清空",
            f"已清空 {count} 个目标的延迟数据，并删除了自动保存的历史文件。")

    def _clear_campus(self) -> None:
        if self._on_clear_campus is None:
            return
        if QMessageBox.question(
            self, "清空额度历史",
            "将丢弃额度快照与日汇总，用量统计与曲线从零开始。\n"
            "设备别名与信任标记会保留。确定吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        count = self._on_clear_campus()
        QMessageBox.information(self, "已清空", f"已删除 {count} 条历史记录。")

    def _accept(self) -> None:
        """关闭前的最后一遍提交。

        正常路径上改动早就即时生效了，这里再走一次是为了兜住"输入框里改到
        一半、还没等到 400ms 防抖就按了关闭"这种情况 —— 不然用户会看到
        界面上写着新值、程序里还是旧值。
        """
        self._live.stop()
        self.commit()
        self.accept()

    def closeEvent(self, event) -> None:  # noqa: N802
        # 按 Esc 或点右上角 × 也要走一遍，理由同 _accept
        self._live.stop()
        self.commit()
        super().closeEvent(event)

    # ------------------------------------------------------------ 落盘

    def commit(self) -> set[str]:
        """把界面上的值写回配置并保存；返回**本次**被改动的分组集合。

        自启动是唯一一个"写出去可能失败"的项（组策略、杀软都可能拦注册表），
        失败时要把开关拨回真实状态 —— 而且那句 `setChecked` 会反过来触发
        toggled 信号，再绕回 commit()。用 _busy 闸门掐掉这个回环。
        """
        if self._busy:
            return set()
        self._busy = True
        try:
            changed = self._commit()
            self.changed |= changed          # changed 是"打开期间一共改过什么"
            return changed
        finally:
            self._busy = False

    def _commit(self) -> set[str]:
        cfg = self.cfg
        campus = cfg.campus
        changed: set[str] = set()

        # ---- 探测 ----
        latency_on = self.ck_latency_enabled.isChecked()
        if latency_on != bool(cfg.settings.get("latency_enabled")):
            cfg.settings["latency_enabled"] = latency_on
            changed.add("latency")

        interval, timeout = self.sp_interval.value(), self.sp_timeout.value()
        if (interval, timeout) != (cfg.interval, cfg.timeout_ms):
            cfg.apply_probe_defaults(interval, timeout)
            changed.add("probe")

        window = self.cb_window.currentData() or 300
        if window != cfg.settings.get("chart_window"):
            cfg.settings["chart_window"] = window
            changed.add("chart_window")

        # ---- 悬浮窗 ----
        oc = cfg.overlay_cfg
        before = dict(oc)
        oc.update({
            "visible": self.ck_ov_show.isChecked(),
            "show_quota": self.ck_ov_quota.isChecked(),
            "show_used": self.ck_ov_used.isChecked(),
            "show_balance": self.ck_ov_balance.isChecked(),
            "show_ping": self.ck_ov_ping.isChecked(),
            "ping_tid": str(self.cb_ov_ping_target.currentData() or ""),
            "show_latency": self.ck_ov_latency.isChecked(),
            "compact": self.ck_ov_compact.isChecked(),
            "locked": self.ck_ov_locked.isChecked(),
            "opacity": self.sp_ov_opacity.value() / 100.0,
            "w": self.sp_ov_width.value(),
        })
        if oc != before:
            changed.add("overlay")

        # ---- 校园网 ----
        campus_before = (
            campus.enabled, campus.account, campus.suffix, campus.portal_url,
            campus.eportal_host, campus.eportal_port, campus.timeout,
            campus.poll_interval, campus.quota_gb, campus.gb_base,
            campus.price_per_gb, campus.quota_source,
        )
        campus.enabled = self.ck_campus_on.isChecked()
        campus.account = self.ed_account.text().strip()
        campus.suffix = self.cb_suffix.currentData() or ""
        campus.portal_url = self.ed_portal.text().strip() or campus.portal_url
        campus.eportal_host = self.ed_eportal_host.text().strip() or campus.eportal_host
        campus.eportal_port = int(self.sp_eportal_port.value())
        campus.timeout = float(self.sp_campus_timeout.value())
        campus.poll_interval = int(self.cb_quota_interval.currentData() or 60)
        campus.quota_gb = float(self.sp_quota_gb.value())
        campus.gb_base = int(self.cb_gb_base.currentData() or 1024)
        campus.price_per_gb = float(self.sp_price.value())
        campus.quota_source = self.cb_quota_source.currentData() or "v4"
        campus_after = (
            campus.enabled, campus.account, campus.suffix, campus.portal_url,
            campus.eportal_host, campus.eportal_port, campus.timeout,
            campus.poll_interval, campus.quota_gb, campus.gb_base,
            campus.price_per_gb, campus.quota_source,
        )
        if campus_before != campus_after:
            changed.add("campus")

        # ---- 提醒 ----
        notify_before = (
            campus.notify_enabled, campus.notify_quota, campus.notify_surge,
            campus.notify_forecast, campus.notify_balance, campus.notify_device,
            campus.warn_left_gb, campus.critical_left_gb, campus.warn_balance_yuan,
            campus.surge_factor, campus.quiet_hours, campus.notify_cooldown,
        )
        campus.notify_enabled = self.ck_notify.isChecked()
        campus.notify_quota = self.ck_notify_quota.isChecked()
        campus.notify_surge = self.ck_notify_surge.isChecked()
        campus.notify_forecast = self.ck_notify_forecast.isChecked()
        campus.notify_balance = self.ck_notify_balance.isChecked()
        campus.notify_device = self.ck_notify_device.isChecked()
        campus.warn_left_gb = float(self.sp_warn_left.value())
        campus.critical_left_gb = float(self.sp_crit_left.value())
        campus.warn_balance_yuan = float(self.sp_warn_balance.value())
        campus.surge_factor = float(self.sp_surge.value())
        campus.notify_cooldown = int(self.sp_cooldown.value())
        self._apply_quiet_hours(campus)

        notify_after = (
            campus.notify_enabled, campus.notify_quota, campus.notify_surge,
            campus.notify_forecast, campus.notify_balance, campus.notify_device,
            campus.warn_left_gb, campus.critical_left_gb, campus.warn_balance_yuan,
            campus.surge_factor, campus.quiet_hours, campus.notify_cooldown,
        )
        if notify_before != notify_after:
            changed.add("notify")

        # ---- 设备 ----
        if (bool(campus.device_check) != self.ck_device.isChecked()
                or int(campus.device_interval) != self.sp_device_interval.value()
                or int(campus.max_devices) != self.sp_max_devices.value()):
            campus.device_check = self.ck_device.isChecked()
            campus.device_interval = int(self.sp_device_interval.value())
            campus.max_devices = int(self.sp_max_devices.value())
            changed.add("device")

        # ---- 启动 ----
        if (bool(cfg.settings.get("close_to_tray", True)) != self.ck_close_tray.isChecked()
                or bool(cfg.settings.get("start_to_tray", False))
                != self.ck_start_tray.isChecked()):
            cfg.settings["close_to_tray"] = self.ck_close_tray.isChecked()
            cfg.settings["start_to_tray"] = self.ck_start_tray.isChecked()
            changed.add("startup")

        want = self.ck_autostart.isChecked()
        if want != autostart.is_enabled():
            ok, err = autostart.set_enabled(want)
            if ok:
                # 注册表写成功了，配置里那份**必须**同步 —— 否则 app 层响应
                # settingsChanged 时会按 cfg 里还是旧值的 autostart 再调一次
                # set_enabled，把用户刚勾上的自启立刻撤掉（勾了=没勾）。
                cfg.settings["autostart"] = want
                changed.add("autostart")
            else:
                self.ck_autostart.setChecked(autostart.is_enabled())
                QMessageBox.warning(
                    self,
                    "开机自启动没设置成功",
                    f"系统拒绝了这次注册表写入：\n{err}\n\n"
                    "常见原因是安全软件或组策略锁了启动项。其它设置不受影响。",
                )

        cfg.save()
        self.changed = changed
        return changed
