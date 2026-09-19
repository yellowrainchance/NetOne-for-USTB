"""枚举本机当前活跃的网络连接，用来找出游戏实际连的服务器。

Windows 的 `netstat -b`（显示进程名）需要管理员权限，但 `netstat -no`（显示 PID）
不需要 —— 所以这里走 "netstat 拿 PID + tasklist 拿进程名" 两步，普通权限即可。

用途：直接盯着官网测延迟只能反映"出境线路"，
真正要排查游戏卡顿，得测你实际连的那台游戏服务器。
"""
from __future__ import annotations

import csv
import io
import re
import subprocess
from dataclasses import dataclass

from .probe import IS_WINDOWS, _NO_WINDOW, _decode

# 浏览器/系统进程：连接数多但基本不是你要找的
NOISE = {
    "chrome.exe", "msedge.exe", "firefox.exe", "iexplore.exe", "opera.exe",
    "brave.exe", "360se.exe", "qqbrowser.exe", "sogouexplorer.exe",
    "svchost.exe", "system", "system idle process", "lsass.exe", "services.exe",
    "searchindexer.exe", "one drive.exe", "onedrive.exe", "dropbox.exe",
    "smartscreen.exe", "runtimebroker.exe", "dllhost.exe", "taskhostw.exe",
    "explorer.exe", "searchapp.exe", "startmenuexperiencehost.exe",
}

_IPV4 = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})$")
_ENDPOINT = re.compile(r"^(.*):(\d+|\*)$")


@dataclass(frozen=True)
class Conn:
    process: str
    pid: int
    proto: str          # TCP | UDP
    remote_ip: str
    remote_port: int
    state: str

    @property
    def addr(self) -> str:
        if ":" in self.remote_ip:      # IPv6 加回方括号，才能配回 host:port 的形式
            return f"[{self.remote_ip}]:{self.remote_port}"
        return f"{self.remote_ip}:{self.remote_port}"

    @property
    def is_lan(self) -> bool:
        return is_private(self.remote_ip)


def is_private(ip: str) -> bool:
    """局域网 / 运营商内网地址。"""
    if ":" in ip:
        low = ip.lower()
        return low.startswith(("fe80", "fc", "fd", "::1"))
    m = _IPV4.match(ip)
    if not m:
        return False
    a, b = (int(x) for x in ip.split(".")[:2])
    if a == 10 or a == 127:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    if a == 169 and b == 254:
        return True
    if a == 100 and 64 <= b <= 127:   # 运营商 CGNAT
        return True
    return False


def _split_endpoint(text: str) -> tuple[str, int] | None:
    """'10.0.0.1:443' / '[2408:...]:443' / '*:*' -> (ip, port)"""
    text = text.strip()
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return None
        host = text[1:end]
        rest = text[end + 1:]
        if not rest.startswith(":"):
            return None
        port_s = rest[1:]
    else:
        m = _ENDPOINT.match(text)
        if not m:
            return None
        host, port_s = m.group(1), m.group(2)
    if not host or host == "*" or port_s == "*":
        return None
    try:
        return host, int(port_s)
    except ValueError:
        return None


def process_names() -> dict[int, str]:
    """PID -> 进程名。tasklist 偶尔很慢，调用方自己缓存。"""
    if not IS_WINDOWS:
        return {}
    try:
        cp = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"], capture_output=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}
    out: dict[int, str] = {}
    reader = csv.reader(io.StringIO(_decode(cp.stdout)))
    for row in reader:
        if len(row) < 2:
            continue
        try:
            out[int(row[1])] = row[0]
        except ValueError:
            continue
    return out


def parse_netstat(text: str) -> list[tuple[str, str, str, str, int]]:
    """解析 netstat -no 输出 -> (proto, local, remote, state, pid)。

    拆成纯函数是为了能用固定文本测试 —— 各系统/语言版本的列对齐不一样。
    UDP 行没有状态列，靠列数区分。
    """
    rows: list[tuple[str, str, str, str, int]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        proto = parts[0].upper()
        if proto not in ("TCP", "UDP", "TCPV6", "UDPV6"):
            continue
        if proto.startswith("UDP"):
            state, pid_s = "", parts[3]
        else:
            if len(parts) < 5:
                continue
            state, pid_s = parts[3], parts[4]
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        rows.append((proto.replace("V6", ""), parts[1], parts[2], state, pid))
    return rows


def _netstat_rows() -> list[tuple[str, str, str, str, int]]:
    """返回 (proto, local, remote, state, pid)。"""
    try:
        cp = subprocess.run(
            ["netstat", "-no"], capture_output=True, timeout=20, creationflags=_NO_WINDOW
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    return parse_netstat(_decode(cp.stdout))


def keep_row(proto: str, ip: str, port: int, state: str, only_established: bool = True) -> bool:
    """这条连接值不值得展示（纯函数，便于测试）。"""
    if port == 0 or ip in ("127.0.0.1", "::1", "0.0.0.0", "::"):
        return False
    if only_established and proto == "TCP" and state != "ESTABLISHED":
        return False
    return True


def list_connections(
    include_lan: bool = True,
    include_noise: bool = True,
    only_established: bool = True,
) -> list[Conn]:
    """列出活跃连接（默认只看已建立、有明确对端的）。"""
    names = process_names()
    seen: set[tuple] = set()
    out: list[Conn] = []

    for proto, _local, remote, state, pid in _netstat_rows():
        parsed = _split_endpoint(remote)
        if parsed is None:
            continue
        ip, port = parsed
        if not keep_row(proto, ip, port, state, only_established):
            continue
        if not include_lan and is_private(ip):
            continue
        proc = names.get(pid, f"PID {pid}")
        if not include_noise and proc.lower() in NOISE:
            continue
        key = (proc, ip, port, proto)
        if key in seen:
            continue
        seen.add(key)
        out.append(Conn(proc, pid, proto, ip, port, state or "UDP"))
    return out


def grouped(conns: list[Conn]) -> list[tuple[str, list[Conn]]]:
    """按进程分组，组内按连接数/端口排序；疑似游戏的排前面。

    "疑似游戏"是个启发式：非浏览器噪声进程、且不是常见网页端口。
    """
    buckets: dict[str, list[Conn]] = {}
    for c in conns:
        buckets.setdefault(c.process, []).append(c)

    def sort_key(item: tuple[str, list[Conn]]):
        proc, items = item
        noisy = proc.lower() in NOISE
        web_only = all(c.remote_port in (80, 443) for c in items)
        # 非噪声、且不全是网页端口的，更可能是游戏/应用自己的连接
        return (noisy, web_only, -len(items), proc.lower())

    result = []
    for proc, items in sorted(buckets.items(), key=sort_key):
        items.sort(key=lambda c: (c.remote_port in (80, 443), c.remote_ip, c.remote_port))
        result.append((proc, items))
    return result
