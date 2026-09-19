"""桌面通知（Windows Toast），带冷却与免打扰。

依赖 windows-toasts；不可用时自动降级到托盘气泡（由 fallback 提供）。
"""

from __future__ import annotations

import time
from datetime import datetime

from core.config import Config

APP_ID = "NetOne"


def parse_hhmm(text: str) -> int | None:
    """'08:30' -> 510（分钟）。解析失败返回 None。"""
    try:
        h, m = text.strip().split(":", 1)
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


class Notifier:
    """按类别去重、可免打扰的通知器。"""

    def __init__(self, cfg: Config, fallback=None):
        self.cfg = cfg
        self.fallback = fallback          # (title, message) -> None
        self._sent: dict[str, float] = {}  # key -> 上次发送时间
        self._toaster = None               # None=未初始化，False=不可用

    # ------------------------------------------------------------ 内部
    def _ensure_toaster(self):
        if self._toaster is None:
            try:
                from windows_toasts import WindowsToaster  # noqa: PLC0415

                self._toaster = WindowsToaster(APP_ID)
            except Exception:
                self._toaster = False
        return self._toaster

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        """是否处于免打扰时段（支持跨午夜，如 23:00-08:00）。"""
        text = (self.cfg.campus.quiet_hours or "").strip()
        if "-" not in text:
            return False
        start_txt, end_txt = text.split("-", 1)
        start, end = parse_hhmm(start_txt), parse_hhmm(end_txt)
        if start is None or end is None:
            return False
        now = now or datetime.now()
        cur = now.hour * 60 + now.minute
        if start <= end:
            return start <= cur < end
        return cur >= start or cur < end

    def _cooling(self, key: str) -> bool:
        last = self._sent.get(key)
        if last is None:
            return False
        return (time.time() - last) < max(0, self.cfg.campus.notify_cooldown) * 60

    # ------------------------------------------------------------ 对外
    def can_notify(self, category: str) -> bool:
        """该类通知是否开启。category 对应 Config 的 notify_* 字段。"""
        if not self.cfg.campus.notify_enabled:
            return False
        return bool(getattr(self.cfg.campus, f"notify_{category}", True))

    def notify(self, key: str, title: str, message: str,
               category: str = "quota", force: bool = False) -> bool:
        """发一条通知。key 用于冷却去重（同类消息不会刷屏）。

        force=True 时忽略免打扰与冷却（用于设置里的「测试通知」）。
        返回是否真的发出。
        """
        if not force:
            if not self.can_notify(category):
                return False
            if self.in_quiet_hours():
                return False
            if self._cooling(key):
                return False

        self._sent[key] = time.time()
        self._emit(title, message)
        return True

    def _emit(self, title: str, message: str) -> None:
        toaster = self._ensure_toaster()
        if toaster:
            try:
                from windows_toasts import Toast  # noqa: PLC0415

                toast = Toast([title, message])
                toaster.show_toast(toast)
                return
            except Exception:
                pass  # 落到下面的托盘气泡
        if self.fallback:
            try:
                self.fallback(title, message)
            except Exception:
                pass

    def reset(self) -> None:
        """清空冷却记录（配置变更后调用）。"""
        self._sent.clear()
