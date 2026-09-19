"""设备监控：拉在线终端、比对上下线、套用别名与信任标记。"""

from __future__ import annotations

from .api import EPortalAPI
from core.config import Config
from .models import Device
from .store import Store

EVENT_ONLINE = "online"
EVENT_OFFLINE = "offline"


class DeviceWatcher:
    """维护「上次可见设备」，每次刷新产出上下线事件。

    首次刷新只建立基线、不产生事件，避免程序刚启动就刷一串历史通知。
    """

    def __init__(self, cfg: Config, api: EPortalAPI, store: Store):
        self.cfg = cfg
        self.api = api
        self.store = store
        self._last: dict[str, Device] = {}
        self._baseline = False
        self._account: str = ""

    # ------------------------------------------------------------ 查询
    def refresh(self, account: str = "", local_ip: str = "") -> tuple[list[Device], list[tuple[str, Device]]]:
        """返回 (当前设备列表, 事件列表)。

        事件为 (EVENT_ONLINE / EVENT_OFFLINE, Device)。
        account 为空时回退到设置账号（cfg）；再为空则不请求、返回空列表。
        账号与上次不同时自动重置基线，避免把旧账号的设备误报为上下线。
        """
        account = (account or self.cfg.campus.full_account() or self.cfg.campus.account or "").strip()
        if not account:
            self._last.clear()
            self._baseline = False
            return [], []
        if account != self._account:
            self.reset()   # 换账号：旧基线作废
        self._account = account

        devices = self.api.find_devices(account, local_ip)
        aliases = self.store.aliases()

        for d in devices:
            self._apply_alias(d, aliases)
            d.is_local = bool(local_ip) and d.ip == local_ip

        events = self._diff(devices)
        for kind, d in events:
            self.store.save_device_event(d.mac, kind, d.ip, d.host)

        self._last = {d.mac: d for d in devices}
        return devices, events

    def _apply_alias(self, device: Device, aliases: dict) -> None:
        key = (device.mac or "").upper()
        # 接口返回的 MAC 可能带冒号或横线，统一后用无分隔形式再找一次
        raw = key.replace(":", "").replace("-", "")
        info = aliases.get(key) or aliases.get(raw)
        if not info:
            return
        device.alias = info.get("alias") or ""
        device.trusted = bool(info.get("trusted"))

    def _diff(self, devices: list[Device]) -> list[tuple[str, Device]]:
        if not self._baseline:
            self._baseline = True
            return []

        current = {d.mac: d for d in devices}
        events: list[tuple[str, Device]] = []
        for mac, d in current.items():
            if mac not in self._last:
                events.append((EVENT_ONLINE, d))
        for mac, d in self._last.items():
            if mac not in current:
                events.append((EVENT_OFFLINE, d))
        return events

    # ------------------------------------------------------------ 辅助
    def unknown(self, devices: list[Device]) -> list[Device]:
        """未信任的设备（可能是蹭网）。"""
        return [d for d in devices if not d.trusted]

    def reset(self) -> None:
        """清空基线（换账号时调用）。"""
        self._last.clear()
        self._baseline = False
