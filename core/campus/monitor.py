"""额度轮询调度器。

从 NetQuota 的 core/monitor.py 平移过来，只改了一处：`collect()` 现在吃整个
`Config`（额度字段在 `cfg.campus` 里），所以这里不再把 cfg 拆开传。

网络请求一律丢进线程池，用信号回主线程，UI 绝不阻塞。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from core.config import Config

from .collector import collect

# 轮询间隔下限。门户页 + 接口各一次请求，30s 已经够轻；
# 再密也不会有新数字，只是白耗门户的连接数。
MIN_INTERVAL = 30


class _Signals(QObject):
    finished = Signal(object)


class _Worker(QRunnable):
    def __init__(self, fn: Callable):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()
        # 禁用线程池自动删除：run() 返回后 finished 的跨线程投递可能还没
        # 走完，此刻被线程池 delete = 接收方收尾阶段摸已释放内存（闪退成因）。
        # 生命周期归调用方：refresh_now 持有到下一轮刷新时替换。
        self.setAutoDelete(False)

    def run(self):  # 在线程池中执行
        try:
            result = self.fn()
        except Exception as e:  # 兜底，避免线程里抛出导致崩溃
            result = e
        self.signals.finished.emit(result)


class CampusMonitor(QObject):
    """定时采集校园网额度并广播结果。"""

    snapshotReady = Signal(object)   # Snapshot
    failed = Signal(str)             # 异常文本

    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._busy = False
        self._pending = False   # 忙时收到刷新请求 → 排队，完成后补刷
        self._worker: _Worker | None = None   # 在飞任务，调用方持有（闪退修复）
        self._pool = QThreadPool.globalInstance()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_now)

    # ---------- 控制 ----------
    def start(self) -> None:
        self.refresh_now()
        self._timer.start(self.interval_ms())

    def stop(self) -> None:
        self._timer.stop()

    def interval_ms(self) -> int:
        return max(MIN_INTERVAL, int(self.cfg.campus.poll_interval)) * 1000

    def set_interval(self, seconds: int) -> None:
        self.cfg.campus.poll_interval = max(MIN_INTERVAL, int(seconds))
        if self._timer.isActive():
            self._timer.start(self.interval_ms())

    def is_running(self) -> bool:
        return self._timer.isActive()

    def pause(self, seconds: int = 600) -> None:
        """临时停一会儿（例如换网络、要手动登录的时候）。到点自动恢复。"""
        self._timer.stop()
        QTimer.singleShot(seconds * 1000,
                          lambda: self._timer.start(self.interval_ms()))

    def refresh_now(self) -> None:
        if self._busy:
            self._pending = True   # 忙则排队，完成后自动补刷一次
            return
        self._busy = True
        worker = _Worker(lambda: collect(self.cfg))
        worker.signals.finished.connect(self._on_done)
        # 持有在飞任务，下一轮刷新时替换：autoDelete(False) 之后生命周期
        # 归调用方，引用的建立与释放都发生在主线程的确定时点（2026-09-22
        # 闪退修复，与 app.py/_task、ui/devices_dialog.py 同一批规矩）。
        self._worker = worker
        self._pool.start(worker)

    # ---------- 回调 ----------
    def _on_done(self, result) -> None:
        self._busy = False
        pending, self._pending = self._pending, False
        if isinstance(result, Exception):
            self.failed.emit(str(result))
        else:
            self.snapshotReady.emit(result)
        if pending:
            self.refresh_now()
