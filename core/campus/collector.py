"""统一采集逻辑：门户首页 + ePortal 接口。

CLI 与 GUI 共用同一份实现，避免两处逻辑漂移。

## 主指标是接口的 v4，不是门户的 flow

门户首页内嵌的 `flow` 变量是**滞后/次级口径**（同刻实测比自助服务权威账本
少 1.2~1.6 GB），而 `visitor/loadUserFlow` 的 `v4` 与权威账本口径一致。
所以本版把 v4 当主指标，flow 只作对照值。

接口失败时会退回 flow 兜底并把 `source` 标成 `portal` —— 数字仍然有
（比没有好），但界面会明说是门户口径，不冒充权威值。
"""

from __future__ import annotations

from core.config import Config

from .api import EPortalAPI
from .models import KB_PER_MB, SOURCE_API, SOURCE_PORTAL, Snapshot
from .portal import PortalClient


def collect(cfg: Config) -> Snapshot:
    """抓一次完整状态。

    主指标（已用额度）来自 ePortal `loadUserFlow` 的 v4；门户首页负责
    在线状态/账号/余额/时长/IP，并顺便带回 flow 作对照。
    """
    campus = cfg.campus
    portal = PortalClient(campus.portal_url, campus.timeout)
    snap = portal.fetch()

    # 账号选填：主指标来自门户当前会话（页面自带 uid），无需预设。
    # 设置账号只在离线拿不到会话账号时兜底。
    if not snap.account:
        snap.account = campus.account

    if snap.online:
        snap.used_mb, snap.v6_mb, snap.source = _query_usage(campus, snap)

    return snap


def _query_usage(campus, snap: Snapshot) -> tuple[float, float, str]:
    """查已用流量。返回 (used_mb, v6_mb, source)。"""
    api = EPortalAPI(campus.eportal_host, campus.eportal_port, campus.timeout)
    account = snap.account or campus.full_account()
    if not account:
        # 连账号都没有（未认证 + 没填设置）：接口没法查，只能退回门户口径
        return _fallback_flow(snap, "没有可用账号")

    try:
        v4_mb, v6_mb = api.load_user_flow(account)
        return max(0.0, v4_mb), max(0.0, v6_mb), SOURCE_API
    except Exception as e:      # 接口失败不阻塞主指标，退回门户口径
        return _fallback_flow(snap, str(e))


def _fallback_flow(snap: Snapshot, why: str) -> tuple[float, float, str]:
    """接口不可用时退回门户 flow。"""
    snap.error = (snap.error + f"；接口流量查询失败：{why}").strip("；")
    if snap.flow_kb > 0:
        return snap.flow_kb / KB_PER_MB, snap.v6_mb, SOURCE_PORTAL
    return 0.0, snap.v6_mb, ""


def collect_devices(cfg: Config, user_ip: str = "", session_account: str = "") -> list:
    """查询在线设备；失败返回空列表。

    账号取「设置固定账号 > 会话账号」（会话账号由调用方传入），都为空则不请求。
    """
    campus = cfg.campus
    account = campus.device_account(session_account)
    if not account:
        return []
    api = EPortalAPI(campus.eportal_host, campus.eportal_port, campus.timeout)
    try:
        return api.find_devices(account, user_ip)
    except Exception:
        return []
