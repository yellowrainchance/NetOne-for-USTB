"""门户首页抓取与解析。

门户首页为 GBK 编码，认证成功后会在 <script> 中注入一批变量，
拿到登录页时这些变量不存在，据此判断是否在线。
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request

from .models import Snapshot

DEFAULT_PORTAL = "http://202.204.48.66"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NetQuota/0.1"


class PortalError(Exception):
    """门户访问或解析失败。"""


class PortalClient:
    """抓取 http://202.204.48.66/ 并解析计费相关变量。"""

    def __init__(self, base_url: str = DEFAULT_PORTAL, timeout: float = 6.0):
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout

    # ---------- 公开接口 ----------
    def fetch(self) -> Snapshot:
        """抓取并解析。网络异常时返回 online=False 的快照，不抛异常。"""
        snap = Snapshot()
        try:
            html = self._get(self.base_url)
        except PortalError as e:
            snap.online = False
            snap.error = str(e)
            return snap

        parsed = self.parse(html)
        if parsed.online and not parsed.account:
            parsed.error = "解析到登录态但未取到账号"
        return parsed

    # ---------- 内部实现 ----------
    def _get(self, url: str) -> str:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise PortalError(f"门户返回 HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise PortalError(f"无法连接门户（{e.reason}）") from e
        except OSError as e:
            raise PortalError(f"网络错误：{e}") from e
        return raw.decode("gbk", errors="ignore")

    @staticmethod
    def parse(html: str) -> Snapshot:
        """从首页 HTML 提取变量。"""
        snap = Snapshot(ts=time.time())

        # 登录页没有 uid= 这个变量，据此判定认证状态
        if "uid='" not in html:
            snap.online = False
            snap.error = "未认证或不在校园网内"
            return snap

        snap.online = True

        def g(pattern: str, cast=str, default=None):
            m = re.search(pattern, html)
            if not m:
                return default
            try:
                return cast(m.group(1).strip())
            except (TypeError, ValueError):
                return default

        # (?<![\w]) 避免匹配到 etime= / oltime= 等
        snap.account = g(r"uid='([^']+)'", str, "")
        snap.minutes = g(r"(?<![\w])time='([^']+)'", int, 0)
        snap.flow_kb = g(r"flow='([^']+)'", int, 0)
        snap.balance_raw = g(r"fee='([^']+)'", int, 0)
        snap.v4ip = g(r"v4ip='([^']*)'", str, "")
        snap.v6ip = g(r"v6ip='([^']*)'", str, "")
        return snap
