"""ePortal JSONP 接口封装（:801/eportal/portal/*）。

所有接口均为 JSONP，无需登录态，仅需账号。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .models import Device

DEFAULT_HOST = "202.204.48.66"
DEFAULT_PORT = 801
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NetQuota/0.1"


class APIError(Exception):
    """接口调用失败。"""


class EPortalAPI:
    """ePortal 接口客户端。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 6.0, js_version: str = "4.1"):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.js_version = js_version

    # ---------- 路径 ----------
    @property
    def portal_api(self) -> str:
        scheme = "https" if self.port == 802 else "http"
        return f"{scheme}://{self.host}:{self.port}/eportal/portal/"

    # ---------- 接口 ----------
    def load_user_flow(self, account: str) -> tuple[float, float]:
        """返回 (v4_mb, v6_mb)。

        v4 = 全部 IPv4 出网流量（含校内免费资源）
        v6 = IPv6 流量（实测为静态值）
        """
        obj = self._jsonp("visitor/loadUserFlow", {"account": account})
        if str(obj.get("result")) not in ("1", "ok"):
            raise APIError(obj.get("msg") or "流量查询失败")
        data = obj.get("data") or {}
        try:
            return float(data.get("v4", 0)), float(data.get("v6", 0))
        except (TypeError, ValueError) as e:
            raise APIError(f"流量字段解析失败：{data!r}") from e

    def find_devices(self, account: str, user_ip: str = "") -> list[Device]:
        """查询账号下在线终端。"""
        obj = self._jsonp(
            "mac/find",
            {
                "user_account": account,
                "login_method": 0,
                "find_mac": 1,
                "wlan_user_ip": user_ip or "0.0.0.0",
                "wlan_user_mac": "000000000000",
            },
        )
        if str(obj.get("result")) not in (1, "1", "ok"):
            raise APIError(obj.get("msg") or "设备查询失败")

        devices: list[Device] = []
        for item in obj.get("list") or []:
            try:
                devices.append(
                    Device(
                        ip=item.get("online_ip", ""),
                        mac=(item.get("online_mac") or "").upper(),
                        host=item.get("dhcp_host", ""),
                        online_time=item.get("online_time", ""),
                        time_long=int(item.get("time_long") or 0),
                        downlink_bytes=int(item.get("downlink_bytes") or 0),
                        uplink_bytes=int(item.get("uplink_bytes") or 0),
                    )
                )
            except (TypeError, ValueError):
                continue
        return devices

    def offline_device(self, account: str, ip: str) -> bool:
        """把指定 IP 踢下线（自服务接口，仅有线网络可用）。"""
        query = urllib.parse.urlencode({"username": account, "ip": ip})
        url = f"http://zifuwu.ustb.edu.cn:8080/OfflineExterAction/quickTooffline?{query}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
            return True
        except (urllib.error.URLError, OSError) as e:
            raise APIError(f"下线失败：{e}") from e

    # ---------- 内部实现 ----------
    def _jsonp(self, path: str, params: dict, callback: str = "dr1001") -> dict:
        query = dict(params)
        query["callback"] = callback
        query["jsVersion"] = self.js_version
        query["lang"] = "zh"
        url = self.portal_api + path + "?" + urllib.parse.urlencode(query)

        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
        except urllib.error.URLError as e:
            raise APIError(f"无法连接 ePortal（{e.reason}）") from e
        except OSError as e:
            raise APIError(f"网络错误：{e}") from e

        try:
            start = text.index("(")
            end = text.rindex(")")
            return json.loads(text[start + 1:end])
        except (ValueError, json.JSONDecodeError) as e:
            raise APIError(f"响应解析失败：{text[:120]!r}") from e
