"""NetOne 入口。

用法：  python app.py            （有控制台，便于排错）
        pythonw app.py          （无控制台窗口）

本文件是合并 NetPing 与 NetQuota 之后的唯一装配点，把两套原来各自独立的
组件树拼成一棵：

    延迟侧：Engine(线程) → StatsHub → LatencyPage / 悬浮窗延迟行
    额度侧：CampusMonitor(线程池) → Store/Stats → CampusPage / 悬浮窗额度环 / 托盘图标
    共用  ：MainWindow(标签页) + OverlayWindow + TrayController + Notifier

两套调度器各跑各的（延迟是 1s 级、额度是 30s+ 级），互不等待，所以合并不引入
任何额外延迟。
"""
from __future__ import annotations

import os
import sys
import time

# 以本文件所在目录为基准，保证 core / ui 能被导入（无论从哪里启动）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# pythonw 启动时 stdout/stderr 是 None，任何 print 都会崩，先兜住
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal  # noqa: E402
from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

from core.campus.api import EPortalAPI  # noqa: E402
from core.campus.devices import EVENT_OFFLINE, EVENT_ONLINE, DeviceWatcher  # noqa: E402
from core.campus.models import Snapshot  # noqa: E402
from core.campus.monitor import CampusMonitor  # noqa: E402
from core.campus.store import Store  # noqa: E402
from core.campus.stats import Stats as CampusStats  # noqa: E402
from core.config import APP_NAME, MAIN_TITLE, OVERLAY_TITLE, Config  # noqa: E402
from core.net.engine import Engine  # noqa: E402
from core.net import history as latency_history  # noqa: E402
from core.net.stats import StatsHub  # noqa: E402
from core.notify import Notifier  # noqa: E402
from ui.devices_dialog import DevicesDialog  # noqa: E402
from ui.icon import icon as make_icon  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.overlay import OverlayWindow  # noqa: E402
from ui.tray import TrayController  # noqa: E402

INSTANCE_KEY = os.environ.get("NETONE_INSTANCE_KEY") or "NetOne-SingleInstance"
# 设了 NETONE_INSTANCE_KEY（自动化测试用）就只在管道域内判重，不再做窗口标题兜底。
# 理由：窗口枚举看的是**整台机器**的顶层窗口，测试机上一份无关的 NetOne
# 就能让被测程序误判"已在运行"然后静默退出 —— 那样测出来的结论全是假的。
WINDOW_SCAN = not os.environ.get("NETONE_INSTANCE_KEY")
# 顶层窗口标题定义在 core/config.py（单一来源），检测时按**完整标题**精确匹配。
# 绝不能用 startswith("NetOne")：资源管理器窗口的标题就是文件夹名，
# 打开 netone 目录时它叫「NetOne - 文件资源管理器」—— 早先的写法会把它认成
# "自己的实例正在运行"，于是双击 exe 直接静默退出，屏幕上什么都不发生。

OVERLAY_WINDOW = 120.0     # 悬浮窗展示最近多少秒的延迟

# 进程级持有 App 实例，防止其被垃圾回收。
# 陷阱：App 名下所有 QObject（两个调度器的 QTimer、托盘、两页界面、信号连接）
# 都没有 Qt parent，唯一引用来自 Python 属性。若 main() 写成
# `App(argv).run()`（临时对象），run() 返回后整棵组件树被回收：
#   - 采集线程稍后带回数据 → 信号已 emit 但接收者已死 → 静默丢失
#   - 调度器的 QTimer 随对象销毁 → 只采一次，永不刷新
# 症状无报错无崩溃，仅表现为"界面永不更新、数据库无写入"。
_APP_HOLDER: list = []  # noqa: N816


def _window_kind(title: str) -> str | None:
    """这个顶层窗口标题是不是 NetOne 自己的窗口？返回 "main" / "overlay" / None。

    纯函数，故意不含 Qt 依赖，方便单测直接喂字符串进来。
    """
    t = (title or "").strip()
    if not t:
        return None
    if t == MAIN_TITLE:
        return "main"
    if t == OVERLAY_TITLE:
        return "overlay"
    return None


def _find_windows() -> tuple[bool, int | None, int | None]:
    """枚举自己的顶层窗口：返回 (是否存在, 主窗口句柄, 悬浮窗句柄)。

    主窗口永远不销毁（关闭只是 hide），所以只要进程活着就枚举得到它。
    """
    found: dict[str, int | None] = {"main": None, "overlay": None}
    if not WINDOW_SCAN:
        return (False, None, None)
    try:
        import win32gui

        def cb(h, _):
            kind = _window_kind(win32gui.GetWindowText(h))
            if kind and found[kind] is None:
                found[kind] = h
                if found["main"] is not None:
                    return False     # 主窗口优先级最高，拿到了就不必再枚举
            return True

        win32gui.EnumWindows(cb, None)
    except ImportError:
        pass
    except Exception:
        # 注销/切换用户等极端情况下 EnumWindows 会抛，有没有它都不该拦住启动
        pass
    return (found["main"] is not None or found["overlay"] is not None,
            found["main"], found["overlay"])


def _focus_window(hwnd: int) -> bool:
    """把窗口抬到前台，返回"看起来成功了没有"。"""
    try:
        import win32gui
        win32gui.ShowWindow(hwnd, 9)             # SW_RESTORE
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass                                  # 前台锁被别人占着，不致命
        return bool(win32gui.IsWindowVisible(hwnd))
    except Exception:
        return False


def _ping_instance(payload: bytes = b"SHOW\n") -> bytes | None:
    """向已在运行的实例发一条命令并读回复；连不上返回 None。

    两件事一起干：
      1. 确认对面真活着 —— named pipe 在进程崩溃后会"幽灵残留"，
         waitForConnected 照样返回 True，所以必须真收发一次。
      2. 请它把主面板抬到前台 —— 新实例没法 show 老实例的窗口。

    读应答要**多等几轮**：老实例收到 SHOW 后要先把自己的主面板显示出来
    （首次 show + 绘制可能上百毫秒），应答必须排在 reliably 能等到的地方。
    实测只等 400ms 一次时，首绘慢的那次会让客户端误判"没有实例"，然后
    双双启动（verify_exe 抓到过：第二个实例 25 秒不退，因为它是真启动了）。
    """
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE_KEY)
    if not sock.waitForConnected(500):
        return None
    sock.write(payload)
    sock.flush()
    reply: bytes | None = None
    for _ in range(3):
        if sock.waitForReadyRead(600):
            reply = bytes(sock.readAll())
            break
    sock.disconnectFromServer()
    return reply


def _warn_already_running() -> None:
    """告诉用户"已经有一个在跑了"。没有这一步，观感就是"双击没反应"。

    GUI 程序（console=False）没有控制台，print 等于没说；
    老实例又把主面板收在托盘里时，屏幕上更是一个像素都不会变。
    """
    text = (f"{APP_NAME} 已经在运行了。\n\n"
            "请点屏幕右下角托盘区的 NetOne 图标打开主面板。")
    try:
        import ctypes
        # MB_ICONINFORMATION | MB_SYSTEMMODAL | MB_SETFOREGROUND
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME,
                                         0x40 | 0x1000 | 0x10000)
    except Exception:
        pass


def _already_running() -> bool:
    """已有实例在跑就把它叫出来，本次启动放弃（返回 True）。

    顺序：先走管道（最可靠，能让老实例自己抬窗口），再退到窗口枚举。
    无论走哪条路，判定"已有实例"之后就一定会给用户一个**看得见**的反应，
    绝不静默退出 —— 静默退出是"exe 启动不了"这个报障的直接成因。
    """
    reply = _ping_instance()
    if reply is None:
        # 刚才若连上了却没有应答，极小概率是老实例正忙（比如正在弹窗）。
        # 再试一次：连不上才真的算"没有实例"。Windows 上管道随进程消失，
        # connect 成功 ≈ 有活进程 —— 宁可多花一秒，不可开出第二个实例
        # （第二个实例会占同一份数据目录，sqlite 和单实例语义全被破坏）。
        reply = _ping_instance()
    if reply is not None:
        if reply.startswith(b"OK"):
            return True                       # 老实例收到 SHOW，会自己抬主面板
        # 有回复但不符合协议（旧版本只回一个字节）：活着，但指望不上它抬窗口
        exists, main_hwnd, _ = _find_windows()
        if exists and main_hwnd is not None and _focus_window(main_hwnd):
            return True
        _warn_already_running()
        return True

    exists, main_hwnd, overlay_hwnd = _find_windows()
    if not exists:
        return False
    if main_hwnd is not None and _focus_window(main_hwnd):
        return True
    # 只剩悬浮窗（主面板收在托盘里、管道又不通）：抬它等于没反应，那就有话直说
    _ = overlay_hwnd
    _warn_already_running()
    return True


# 服务端持有探测连接引用，避免 QLocalSocket 被 GC 后收不到数据。
_PROBE_CONNS: set = set()  # noqa: N816


def _listen_instance(app) -> QLocalServer:
    """占住单实例管道，并应答其他实例的探测 / 求显请求。

    必须在主窗口创建之前调用：窗口出现前没有可枚举的窗口标题，若此时不占住
    管道，第二个实例（窗口枚举不到、管道也连不上）会漏检 → 双双启动。

    协议：收到的数据里含 "SHOW" 就把主面板抬到前台，然后一律回 "OK\\n"。
    为什么需要 SHOW：第二个实例没法 show 老实例的窗口，它只能请求。
    没有这一步，用户双击第二次时屏幕上什么都不会发生。
    """
    QLocalServer.removeServer(INSTANCE_KEY)
    server = QLocalServer()

    def _on_new_connection() -> None:
        conn = server.nextPendingConnection()
        if conn is None:
            return
        _PROBE_CONNS.add(conn)

        def _on_ready() -> None:
            data = bytes(conn.readAll())
            # 应答必须**先**发：抬窗（首次 show + 绘制）是耗时动作，排在前面的
            # 话，客户端等应答的窗口（秒级）可能被吃掉 —— 它就会误判"没有
            # 实例在跑"而把自己启动起来，双实例就是这么来的。
            try:
                conn.write(b"OK\n")
                conn.flush()
            except Exception:
                pass
            if b"SHOW" in data:
                # 晚绑定：_listen_instance() 在 App 之前就装好了，那时还没有主窗口。
                # singleShot(0)：抬窗挪到下一轮事件循环，不占着 socket 回调做重 UI。
                cb = getattr(app, "_instance_show_main", None)
                if cb is not None:
                    QTimer.singleShot(0, cb)

        conn.readyRead.connect(_on_ready)
        conn.disconnected.connect(lambda: _PROBE_CONNS.discard(conn))

    server.newConnection.connect(_on_new_connection)
    if not server.listen(INSTANCE_KEY):
        print("警告：单实例管道绑定失败，可能出现多个实例。")
    app._instance_server = server  # noqa: SLF001  保持引用，避免被回收
    return server


class _Signals(QObject):
    finished = Signal(object)


class _Task(QRunnable):
    """把阻塞调用丢进线程池，结果用信号送回主线程。

    延迟侧和额度侧各有一套自己的调度器；这一份只服务于 app 层自己发起的一次性
    调用（设备查询），所以单独放在这里而不是塞进任一侧的模块。
    """

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self):
        try:
            result = self.fn()
        except Exception as e:   # 兜底，绝不让线程里的异常崩掉程序
            result = e
        self.signals.finished.emit(result)


def _task(fn, on_done) -> _Task:
    """起一个后台任务，完成后把结果交给 on_done（主线程执行）。"""
    task = _Task(fn)
    task.signals.finished.connect(on_done)
    QThreadPool.globalInstance().start(task)
    return task


class App:
    """组装两套子系统，集中处理采集后的落库、刷新与告警。"""

    def __init__(self, qapp: QApplication):
        self.qapp = qapp
        # 关窗不退出：主面板收进托盘、悬浮窗留着，退出只走托盘菜单或「退出」按钮
        self.qapp.setQuitOnLastWindowClosed(False)
        self.qapp.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)
        self.icon = make_icon()
        self.qapp.setWindowIcon(self.icon)

        self.cfg = Config()
        self._local_ip = ""
        self._device_dlg: DevicesDialog | None = None

        # ---------------- 延迟侧 ----------------
        self.hub = StatsHub()
        for target in self.cfg.targets:
            self.hub.register(target.id, target.name, target.color)
        self.engine = Engine(self.hub)

        # ---------------- 额度侧 ----------------
        self.quota = self.cfg.quota()
        self.store = Store()
        self.cstats = CampusStats(self.store, self.quota, self.cfg.campus.surge_factor)
        self.api = EPortalAPI(self.cfg.campus.eportal_host,
                              self.cfg.campus.eportal_port,
                              self.cfg.campus.timeout)
        self.cmon = CampusMonitor(self.cfg)
        self.watcher = DeviceWatcher(self.cfg, self.api, self.store)

        # ---------------- 界面 ----------------
        self.tray = TrayController(self.cfg)
        self.notifier = Notifier(self.cfg, fallback=self.tray.notify)
        self.overlay = OverlayWindow(self.cfg)
        self.overlay.setWindowIcon(self.icon)
        self.window = MainWindow(self.cfg, self.hub, self.engine,
                                 campus_stats=self.cstats, campus_store=self.store,
                                 overlay=self.overlay)
        self.window.setWindowIcon(self.icon)

        self.cstats.refresh()
        self.window.campus.update_history(self.cstats.history(30))
        if self.store.latest():
            self.window.campus.update_snapshot(self.store.latest())

        self._connect()
        self._start()

    # ------------------------------------------------------------ 装配

    def _connect(self) -> None:
        # 额度采集
        self.cmon.snapshotReady.connect(self.on_snapshot)
        self.cmon.failed.connect(self.on_failed)

        # 页面 → app
        self.window.campus.refreshRequested.connect(self.cmon.refresh_now)
        self.window.openDevicesRequested.connect(self.open_devices)
        self.window.testNotifyRequested.connect(self._test_notify)
        self.window.settingsChanged.connect(self.on_settings_changed)
        self.window.quitRequested.connect(self.quit)
        self.window.hiddenToTray.connect(self._notify_tray)
        # 延迟页的「开启监测」按钮 → 主开关持久化切换（唯一写入口在 app 层）
        self.window.latency.enableRequested.connect(self.set_latency_enabled)
        # 主窗口的「悬浮窗」按钮 → 显隐。这条连接从装配那天起就漏了：
        # 点按钮毫无反应，设置面板取消「显示悬浮窗」也只同步了按钮状态、
        # 悬浮窗纹丝不动（信号发出去没人接）—— 用户感知就是"关不掉"。
        self.window.overlayToggleRequested.connect(self.set_overlay_visible)

        # 悬浮窗 → app
        self.overlay.openMainRequested.connect(self.show_main)
        self.overlay.focusTargetRequested.connect(self.focus_target)
        self.overlay.openQuotaRequested.connect(self.show_campus)
        self.overlay.quitRequested.connect(self.quit)
        # 拖动/调宽落盘后，把新宽度同步给开着的设置对话框 —— 不然对话框里的
        # 旧值会在用户下次改动任何设置时把刚拖好的宽度写回去。
        self.overlay.geometryChanged.connect(self._on_overlay_geometry_changed)

        # 托盘 → app
        self.tray.openMainRequested.connect(self.show_main)
        self.tray.refreshQuotaRequested.connect(self.cmon.refresh_now)
        self.tray.pauseToggled.connect(self.set_paused)
        self.tray.overlayToggled.connect(self.set_overlay_visible)
        self.tray.openDevicesRequested.connect(self.open_devices)
        self.tray.openSettingsRequested.connect(self.window.open_settings)
        self.tray.quitRequested.connect(self.quit)

    def _start(self) -> None:
        self.store.prune(keep_months=12)

        # 首次运行 / 配置里没有目标时，load() 在内存里补了默认值。
        # 这里落一次盘，让 config.json 和程序实际在用的内容一致 ——
        # 否则用户打开文件会发现 targets 是空的，而界面上明明有 5 个目标。
        if self.cfg.regenerated:
            self.cfg.save()
            self._note("已写入默认配置（含自动探测到的网关）")

        # 把上次退出时保存的延迟历史读回来。必须在 engine.start() 之前：
        # 先恢复、再开采样，否则刚恢复的样本会被新一轮的首屏刷新画出断档。
        self._restore_history()

        # 延迟监测主开关：默认关闭，程序一进来是"纯校园网检测"。
        # 开启与否在延迟页按钮 / 设置里切换（set_latency_enabled 持久化）。
        latency_on = bool(self.cfg.settings.get("latency_enabled"))
        if latency_on:
            self.engine.start(self.cfg.enabled_targets())
        else:
            self._note("延迟监测当前关闭；校园网额度监测不受影响，"
                       "可在「网络延迟」页点「开启监测」打开")
        self.window.latency.set_master_enabled(latency_on)
        self.cmon.start()

        # 悬浮窗刷新：两页的数据都从内存里取，250ms 足够跟手又不费 CPU
        self.ov_timer = QTimer(self.qapp)
        self.ov_timer.setInterval(250)
        self.ov_timer.timeout.connect(self._refresh_overlay)
        self.ov_timer.start()

        # 设备监控
        self.dev_timer = QTimer(self.qapp)
        self.dev_timer.timeout.connect(self.refresh_devices)
        self._sync_device_timer()
        if self.cfg.campus.device_check:
            QTimer.singleShot(3000, self.refresh_devices)

        oc = self.cfg.overlay_cfg
        if bool(oc.get("visible", True)):
            self.overlay.show()
        self.tray.set_overlay_checked(bool(oc.get("visible", True)))

        if not self.cfg.settings.get("start_to_tray"):
            self.window.show()
        else:
            self.window.hide()
        # 启动落页：默认 campus（一进来是校园网检测），并兑现 last_tab 记忆。
        # 这行以前漏了 —— 记忆只写不读，无论上次停在哪页，启动永远停在延迟页。
        self.window.restore_tab()

    def _sync_device_timer(self) -> None:
        campus = self.cfg.campus
        if campus.device_check:
            self.dev_timer.setInterval(max(60, int(campus.device_interval)) * 1000)
            if not self.dev_timer.isActive():
                self.dev_timer.start()
        else:
            self.dev_timer.stop()

    # ------------------------------------------------------------ 延迟历史
    #
    # 延迟样本只在内存里（`TargetStats.samples` 是个 7200 条的环形缓冲），
    # 退出就没了。用户要求「退出时自动保存」，所以退出路径上落一次盘、
    # 启动路径上读回来。文件本身也有上限（同一批 7200 条/目标），
    # 所以"跑得越久占得越多"这件事不会发生在磁盘上，也不会发生在内存里。

    def _restore_history(self) -> None:
        """读回上次退出时保存的延迟历史。

        纯锦上添花：文件被删、被改坏、格式对不上都只记一条日志，
        绝不影响启动 —— `history.load()` 内部已经把异常都吞成返回值了。
        """
        info = latency_history.load(self.cfg, self.hub, self.engine)
        if info.get("error"):
            self._note(f"延迟历史没读进来（{info['error']}），本次从零开始", "warn")
            return
        if not info.get("samples"):
            return
        saved = info.get("saved_at") or 0.0
        when = time.strftime("%m-%d %H:%M", time.localtime(saved)) if saved else "未知时间"
        self._note(f"已恢复上次延迟记录：{info['targets']} 个目标 / "
                   f"{info['samples']} 条样本 / {info['events']} 条事件（存于 {when}）")
        # 只把尾部几张贴回日志框。几百条一次性刷上去，用户要的信息反而被淹掉
        for ev in info.get("log_tail") or []:
            self.window.append_event(f"〔上次会话〕{ev.text}", ev.severity)

    def _save_history(self) -> dict:
        info = latency_history.save(self.cfg, self.hub, self.engine)
        if not info.get("ok"):
            self._note(f"延迟历史保存失败：{info.get('error')}", "warn")
        return info

    # ------------------------------------------------------------ 额度采集

    def on_snapshot(self, snap: Snapshot) -> None:
        try:
            if not isinstance(snap, Snapshot):
                self._note(f"额度采集返回了非预期对象：{type(snap).__name__}", "warn")
                return
            # 先落库，再判清零（判清零依赖历史快照）
            self.store.save_snapshot(snap)
            if snap.online:
                if self.store.check_month_reset(snap):
                    self.store.reset_today(snap)
                    self.notifier.notify("month_reset", "新的计费周期",
                                         "已切换到本月额度，用量重新统计",
                                         category="quota", force=True)
                self.store.upsert_daily(snap)
                self._local_ip = snap.v4ip or self._local_ip

            self.cstats.refresh()
            self.window.campus.update_snapshot(snap)
            self.window.campus.update_history(self.cstats.history(30))
            self._refresh_overlay_quota(snap)

            if snap.online:
                self.check_alerts(snap)
        except Exception as e:   # PySide6 会静默吞掉槽里的异常，这里自己兜住并留痕
            from core.config import data_dir, rotate_log_if_needed
            import traceback
            try:
                rotate_log_if_needed()   # 超限先归档，data/ 里永远只留 latest TXT
                with open(os.path.join(data_dir(), "netone.log"), "a",
                          encoding="utf-8") as fh:
                    fh.write(traceback.format_exc())
            except OSError:
                pass
            self._note(f"额度处理出错：{type(e).__name__}: {e}", "bad")

    def on_failed(self, error: str) -> None:
        self._note(f"额度采集失败：{error}", "warn")
        snap = Snapshot(online=False, error=error, account=self.cfg.campus.full_account())
        self.window.campus.update_snapshot(snap)
        self._refresh_overlay_quota(snap)
        self.window.campus.statusChanged.emit("校园网：离线")

    # ------------------------------------------------------------ 告警

    def check_alerts(self, snap: Snapshot) -> None:
        """按配置逐项检查并发送提醒（同类消息由 Notifier 做冷却）。"""
        campus = self.cfg.campus
        if not campus.notify_enabled:
            return
        quota = self.cfg.quota()
        left = quota.left_gb(snap)

        if campus.notify_quota:
            if left <= campus.critical_left_gb:
                self.notifier.notify(
                    "quota_critical", "流量额度告急",
                    f"本月仅剩 {left:.2f} GB（已用 {quota.used_percent(snap):.1f}%），"
                    f"超额按 {campus.price_per_gb:.2f} 元/GB 计费",
                    category="quota")
            elif left <= campus.warn_left_gb:
                self.notifier.notify(
                    "quota_warn", "流量余量提醒",
                    f"本月剩余 {left:.2f} GB，已用 {quota.used_percent(snap):.1f}%",
                    category="quota")

        if campus.notify_balance and snap.balance_yuan < campus.warn_balance_yuan:
            self.notifier.notify(
                "balance", "账户余额不足",
                f"余额 ¥{snap.balance_yuan:.2f}，低于阈值 "
                f"¥{campus.warn_balance_yuan:.2f}；余额为零将无法认证",
                category="balance")

        if campus.notify_surge:
            surged, today, baseline = self.cstats.surge()
            if surged and baseline > 0:
                self.notifier.notify(
                    "surge", "用量异常偏高",
                    f"今日已用 {today:.2f} GB，是近 7 日日均 {baseline:.2f} GB 的 "
                    f"{today / baseline:.1f} 倍，建议检查是否有后台更新或陌生设备",
                    category="surge")

        if campus.notify_forecast:
            forecast = self.cstats.forecast_gb(snap)
            if forecast > campus.quota_gb:
                over = forecast - campus.quota_gb
                self.notifier.notify(
                    "forecast", "按当前速度会超出额度",
                    f"预计月底 {forecast:.1f} GB，超出 {over:.1f} GB，"
                    f"约需 ¥{quota.over_fee(forecast):.2f}",
                    category="forecast")

    # ------------------------------------------------------------ 设备

    def session_account(self) -> str:
        """当前在线会话账号：最近一次在线快照记录的账号。

        自动跟随模式用：设置账号留空时，设备管理以它作为查询对象，无需手动填写。
        离线/未认证时返回空串（调用方据此跳过本轮查询）。
        """
        last = self.store.latest()
        if last and last.online and last.account:
            return last.account
        return ""

    def refresh_devices(self) -> None:
        campus = self.cfg.campus
        if not campus.device_check:
            return
        account = campus.device_account(self.session_account())
        if not account:
            return   # 无固定账号且当前无在线会话：没有可查的对象，静默跳过
        ip = self._local_ip

        def work():
            return self.watcher.refresh(account=account, local_ip=ip)

        _task(work, self.on_devices)

    def on_devices(self, result) -> None:
        if isinstance(result, Exception):
            return   # 设备查询失败不打扰用户，静默跳过
        devices, events = result
        self.window.campus.set_device_count(len(devices), self.cfg.campus.max_devices)
        self.tray.set_device_hint(len(devices), self.cfg.campus.max_devices)
        self._notify_device_events(devices, events)

    def _notify_device_events(self, devices: list, events: list) -> None:
        campus = self.cfg.campus
        for kind, d in events:
            name = d.alias or d.host or d.ip
            if kind == EVENT_ONLINE:
                if d.trusted:
                    continue
                self._note(f"新设备接入：{name}（{d.ip}）", "warn")
                self.notifier.notify(
                    f"device_on:{d.mac}", "发现新设备接入",
                    f"{name}（{d.ip}）已上线，如不是你本人操作请检查",
                    category="device")
            elif kind == EVENT_OFFLINE:
                self._note(f"设备已下线：{name}（{d.ip}）")
                self.notifier.notify(
                    f"device_off:{d.mac}", "设备已下线",
                    f"{name}（{d.ip}）已断开", category="device")

        limit = max(1, campus.max_devices)
        if len(devices) > limit:
            self.notifier.notify(
                "device_limit", "在线终端数超限",
                f"当前 {len(devices)} 台在线，超过上限 {limit} 台，"
                f"新设备可能无法正常登录",
                category="device")

    def open_devices(self) -> None:
        if self._device_dlg is None or not self._device_dlg.isVisible():
            self._device_dlg = DevicesDialog(
                self.cfg, self.store, notifier=self.notifier,
                local_ip=self._local_ip, session_account=self.session_account())
        self._device_dlg.show()
        self._device_dlg.raise_()
        self._device_dlg.activateWindow()

    # ------------------------------------------------------------ 设置联动

    def on_settings_changed(self, changed) -> None:
        changed = set(changed or ())
        campus = self.cfg.campus

        if "campus" in changed:
            self.quota = self.cfg.quota()
            self.cstats.quota = self.quota
            self.cstats.surge_factor = campus.surge_factor
            self.api = EPortalAPI(campus.eportal_host, campus.eportal_port,
                                  campus.timeout)
            self.watcher.api = self.api
            self.watcher.cfg = self.cfg
            self.cmon.set_interval(campus.poll_interval)
            if campus.enabled:
                if not self.cmon.is_running():
                    self.cmon.start()
            else:
                self.cmon.stop()
            # 改了账号/额度/间隔后立即按新配置重采一次，不必等下一轮轮询
            self.cmon.refresh_now()
            self.window.campus.update_history(self.cstats.history(30))

        if "notify" in changed or "campus" in changed:
            self.notifier.cfg = self.cfg
            self.notifier.reset()

        if "device" in changed or "campus" in changed:
            self.watcher.reset()
            self._sync_device_timer()
            if campus.device_check:
                self.refresh_devices()

        if "latency" in changed:
            # 设置里的「启用延迟监测」复选框：值已在 cfg 里，这里统一走
            # 同一条生效路径（启停引擎 + 刷页面 + 刷托盘），不留第二条。
            self.set_latency_enabled(bool(self.cfg.settings.get("latency_enabled")))

        if "autostart" in changed:
            from core import autostart
            ok, msg = autostart.set_enabled(bool(self.cfg.settings.get("autostart")))
            if not ok:
                self.cfg.settings["autostart"] = False
                self.cfg.save()
                self.window.append_event(f"开机自启动设置失败：{msg}", "warn")
                self.tray.notify("NetOne", msg)

    def set_latency_enabled(self, on: bool) -> None:
        """延迟（Ping）监测主开关：唯一的持久化写入口。

        页面按钮和设置复选框都汇到这里 —— 先动引擎、再刷界面（顺序反了
        状态行会把"已暂停"闪一下），最后落盘。校园网额度监测不受影响。
        """
        on = bool(on)
        if on:
            self.engine.start(self.cfg.enabled_targets())
        else:
            self.engine.stop()
        self.cfg.settings["latency_enabled"] = on
        self.cfg.save()
        self.window.latency.set_master_enabled(on)
        self.tray.set_paused(self.window.is_paused())
        if on:
            self._note("延迟监测已开启（重启后保持）")
        else:
            self._note("延迟监测已关闭（校园网额度监测不受影响）")

    def _test_notify(self) -> None:
        self.notifier.notify("test", "NetOne 测试通知",
                             "如果你看到这条消息，说明通知功能正常。",
                             category="quota", force=True)

    # ------------------------------------------------------------ 悬浮窗

    def set_overlay_visible(self, visible: bool) -> None:
        if visible:
            self.overlay.apply_settings()
            self.overlay.show()
            self.overlay.raise_()
            self._refresh_overlay()
        else:
            self.overlay.hide()
        self.cfg.overlay_cfg["visible"] = visible
        self.cfg.save()
        for widget in (self.window.btn_overlay, self.tray.act_overlay):
            if widget is not None and widget.isChecked() != visible:
                widget.blockSignals(True)
                widget.setChecked(visible)
                widget.blockSignals(False)
        # 开着的设置对话框也要同步，否则它显示的是假状态，用户下一次
        # 在里面的操作会按假状态覆盖回去（"悬浮窗关不掉"的同族问题）。
        dlg = getattr(self.window, "settings_dlg", None)
        if dlg is not None:
            dlg.sync_overlay_visible(visible)

    def _on_overlay_geometry_changed(self) -> None:
        """悬浮窗拖动/调宽落盘后，把新宽度喂给开着的设置对话框。

        读刚落盘的 oc["w"] 而不是 width()：_persist 是宽度的权威落点，
        窗口实际尺寸要等下一次 _apply_size 才跟上，二者可能短暂不一致。
        """
        dlg = getattr(self.window, "settings_dlg", None)
        if dlg is not None:
            dlg.sync_overlay_width(int(self.overlay.cfg.overlay_cfg.get("w") or 0))

    def _refresh_overlay_quota(self, snap: Snapshot | None) -> None:
        """把额度换算成悬浮窗/托盘需要的那几个数。"""
        info = self.quota_info(snap)
        self.overlay.set_quota(info)
        self.tray.update_quota(info)

    def quota_info(self, snap: Snapshot | None = None) -> dict:
        campus = self.cfg.campus
        if snap is None:
            snap = self.store.latest()
        if snap is None:
            return {"online": False, "error": "尚未采集", "total_gb": campus.quota_gb,
                    "balance_yuan": None}
        quota = self.cfg.quota()
        return {
            "left_gb": quota.left_gb(snap),
            "pct_left": quota.left_percent(snap),
            "used_gb": snap.used_gb,
            "total_gb": campus.quota_gb,
            "online": snap.online,
            "source": snap.source,
            "error": snap.error,
            "price": campus.price_per_gb,
            "balance_yuan": snap.balance_yuan if snap.online else None,
        }

    def _refresh_overlay(self) -> None:
        if not self.overlay.isVisible():
            return
        # 引擎没在跑（主开关关闭 / 已暂停）时喂空行：悬浮窗的延迟行和
        # 「当前延迟」会自动隐藏，而不是把最后一轮的旧数字一直挂在那里误导人。
        rows = (self.window.overlay_rows(OVERLAY_WINDOW)
                if self.engine.running else [])
        self.overlay.set_rows(rows)
        self.overlay.set_quota(self.quota_info())
        self.tray.update_network(self.window.latency_status_text())

    # ------------------------------------------------------------ 行为

    def show_main(self) -> None:
        self.window.show_and_raise()

    def show_campus(self) -> None:
        self.window.show_and_raise()
        self.window.show_campus()

    def focus_target(self, tid: str) -> None:
        self.window.show_and_raise()
        self.window.focus_target(tid)

    def set_paused(self, paused: bool) -> None:
        self.window.pause(paused)
        self.tray.set_paused(paused)
        self.window.append_event("延迟监测已暂停" if paused else "延迟监测已恢复")

    def _notify_tray(self) -> None:
        self.tray.tip_once(
            "NetOne 仍在后台运行",
            "监测没有停，悬浮窗也还在。要从托盘图标重新打开主面板，或在那里退出。",
        )

    def _note(self, text: str, severity: str = "info") -> None:
        self.window.append_event(text, severity)

    def quit(self) -> None:
        """退出：先掐掉所有定时器和后台采集，再关库。

        顺序不能反。定时器是挂在事件循环上的，只要循环还在转（比如正在跑一个
        模态对话框），它们就还会被推一次；如果那时数据库已经关了，
        就会在退出画面上弹一个没人看得懂的 sqlite3 报错。
        先 stop 定时器，再 close 库，这个窗口就不存在了。

        延迟历史的落盘放在 stop 之后、close 之前：这时没有新样本再进来
        （存下来的就是最终状态），而程序还没开始拆自己的零件。
        """
        try:
            self.ov_timer.stop()
            self.dev_timer.stop()
            self.cmon.stop()
            self.engine.stop()
            self._save_history()
            self.window.save_geometry()
            self.overlay._persist()      # noqa: SLF001  悬浮窗位置/尺寸
            self.cfg.save()
            self.store.close()
        finally:
            self.tray.hide()
            self.qapp.quit()


def main() -> int:
    # 数据目录升级迁移必须最先做：Config.load() 会去 data/ 找 config.json，
    # 不先迁移的话旧配置还散在运行目录根，会被当成首运行重新生成一套默认值。
    from core.config import LOG_NAME, data_dir, migrate_legacy_data

    migrate_legacy_data()

    # pythonw / 打包运行态下 stdout、stderr 是 None（前面已用 devnull 兜底）。
    # 迁移完再把它们接到 data/netone.log 上：默认的 excepthook 会把未捕获异常
    # 打到 stderr —— 没这一步，崩溃原因就永远没人知道。必须在迁移之后，
    # 否则这个句柄会锁住运行目录根的旧 netone.log，迁移搬不动它。
    try:
        log_path = os.path.join(data_dir(), LOG_NAME)
        if sys.stdout is None or getattr(sys.stdout, "name", "") == os.devnull:
            sys.stdout = open(log_path, "a", buffering=1,
                              encoding="utf-8", errors="replace")
        if sys.stderr is None or getattr(sys.stderr, "name", "") == os.devnull:
            sys.stderr = sys.stdout
    except OSError:
        pass

    # QApplication 必须最先存在：_already_running() 里的 QLocalSocket 和后面所有的
    # QObject 都依赖它。也因此 App 只接收现成的实例，不自己再造一个。
    qapp = QApplication(sys.argv)
    qapp.setApplicationName("NetOne")
    icon = make_icon()
    qapp.setWindowIcon(icon)

    if _already_running():
        print("NetOne 已在运行中，已请它把主面板抬到前台。")
        return 0

    _listen_instance(qapp)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("警告：当前环境不支持系统托盘，程序将只以窗口方式运行。")

    controller = App(qapp)
    _APP_HOLDER.append(controller)   # 关键：持有引用，防止组件树被 GC（见文件头注释）
    # 晚绑定：管道在 App 之前就装好了，但"抬主面板"这个动作只有 App 会做
    qapp._instance_show_main = controller.show_main  # noqa: SLF001
    return qapp.exec()


if __name__ == "__main__":
    sys.exit(main())
