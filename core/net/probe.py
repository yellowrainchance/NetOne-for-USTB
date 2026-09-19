"""单次网络探测：TCP 握手测速 与 ICMP ping。

TCP 模式：先做一次 DNS 解析（单独计时），再对解析出的 IP 做 TCP 握手计时。
          这样得到的 rtt 是纯网络往返，不受 DNS 缓存/解析波动污染。
ICMP 模式：调用系统 ping 命令，无需管理员权限，兼容性最好。

两种模式都不需要管理员权限。
"""
from __future__ import annotations

import concurrent.futures
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

IS_WINDOWS = sys.platform.startswith("win")
_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


@dataclass
class ProbeResult:
    """一次探测的结果。ok=False 时 rtt 为 None，视为丢包。"""

    ok: bool
    rtt: float | None = None          # 毫秒
    dns_ms: float | None = None       # DNS 解析耗时（仅 TCP 模式）
    status: str = "ok"                # ok | timeout | unreachable | dns_fail | error
    detail: str = ""                  # 附加信息，如解析到的 IP 或错误摘要
    gateway: bool = False             # 是否由网关级探测产生（用于诊断归类）


def _decode(raw: bytes) -> str:
    """中文 Windows 的 ping 输出是 OEM 代码页，需要多编码尝试。"""
    for enc in ("mbcs", "gbk", "utf-8"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------- ICMP

_RTT_RE = re.compile(r"(?:时间|time)\s*([=<])\s*(\d+(?:\.\d+)?)\s*ms", re.I)
_TIMEOUT_RE = re.compile(r"请求超时|timed out", re.I)
_UNREACH_RE = re.compile(
    r"无法访问|找不到主机|无法解析|一般故障|unreachable|could not find|could not resolve|general failure",
    re.I,
)


def probe_icmp(host: str, timeout_ms: int = 1500, ipv4: bool = True) -> ProbeResult:
    cmd = ["ping", "-n", "1", "-w", str(int(timeout_ms))]
    if ipv4:
        cmd.append("-4")
    cmd.append(host)
    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_ms / 1000.0 + 3.0,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(False, status="timeout", detail="ping 进程超时")
    except (FileNotFoundError, OSError) as exc:
        return ProbeResult(False, status="error", detail=f"无法执行 ping: {exc}")

    text = _decode(cp.stdout) + _decode(cp.stderr)
    m = _RTT_RE.search(text)
    if m:
        op, val = m.group(1), float(m.group(2))
        # "<1ms" 系统不给出精确值，取 0.5ms 表示"低于 1ms"
        return ProbeResult(True, 0.5 if op == "<" else val, None, "ok", host)

    if _TIMEOUT_RE.search(text):
        return ProbeResult(False, status="timeout", detail="请求超时")
    if _UNREACH_RE.search(text):
        return ProbeResult(False, status="unreachable", detail="目标不可达或域名无法解析")
    return ProbeResult(False, status="error", detail=(text.strip().splitlines() or [""])[0][:80])


# ---------------------------------------------------------------- DNS

# getaddrinfo 是阻塞且**不可中断**的：socket.setdefaulttimeout() 对它无效，
# Windows 上底层走 GetAddrInfoW，网络异常或 DNS/代理配置混乱时能卡几十秒。
# 裸调它会把整个探测线程挂住 —— 界面上就表现为"这个目标一直不更新"。
#
# 所以放到一个固定大小的线程池里等：超时就放弃这一次的读数（返回 dns_fail），
# 而卡住的那个线程自己跑完会归还给池，不会无限堆积。
DNS_TIMEOUT_S = 3.0
_DNS_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="dns"
)


def resolve(
    host: str,
    port: int,
    family: int = socket.AF_INET,
    timeout: float = DNS_TIMEOUT_S,
) -> tuple[list, float]:
    """带超时的 DNS 解析，返回 (getaddrinfo 结果, 耗时毫秒)。

    超时抛 concurrent.futures.TimeoutError，解析失败抛 socket.gaierror。
    """
    t0 = time.perf_counter()
    future = _DNS_POOL.submit(
        socket.getaddrinfo, host, port, family, socket.SOCK_STREAM
    )
    infos = future.result(timeout=timeout)
    return infos, (time.perf_counter() - t0) * 1000.0


# ---------------------------------------------------------------- TCP

def probe_tcp(
    host: str,
    port: int = 443,
    timeout_ms: int = 1500,
    ipv4: bool = True,
    dns_timeout_ms: int | None = None,
) -> ProbeResult:
    family = socket.AF_INET if ipv4 else socket.AF_UNSPEC
    budget = (dns_timeout_ms / 1000.0) if dns_timeout_ms else DNS_TIMEOUT_S
    try:
        infos, dns_ms = resolve(host, port, family, budget)
    except concurrent.futures.TimeoutError:
        return ProbeResult(False, dns_ms=budget * 1000.0, status="dns_fail",
                           detail=f"DNS 解析超时（>{budget:.1f}s）")
    except socket.gaierror as exc:
        return ProbeResult(False, status="dns_fail", detail=f"DNS 解析失败 ({exc.errno})")
    except OSError as exc:
        return ProbeResult(False, status="error", detail=f"解析异常: {exc}")
    if not infos:
        return ProbeResult(False, dns_ms=dns_ms, status="dns_fail", detail="DNS 未返回可用地址")

    last = "timeout"
    last_detail = ""
    for fam, stype, proto, _canon, sockaddr in infos[:3]:
        sock = None
        t1 = time.perf_counter()
        try:
            sock = socket.socket(fam, stype, proto)
            sock.settimeout(timeout_ms / 1000.0)
            sock.connect(sockaddr)
            rtt = (time.perf_counter() - t1) * 1000.0
            return ProbeResult(True, rtt, dns_ms, "ok", str(sockaddr[0]))
        except socket.timeout:
            last, last_detail = "timeout", "TCP 握手超时"
        except OSError as exc:
            last = "unreachable"
            last_detail = f"握手被拒/不可达 (errno={exc.errno})"
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    return ProbeResult(False, dns_ms=dns_ms, status=last, detail=last_detail)


# ---------------------------------------------------------------- 分发

def probe_once(target) -> ProbeResult:
    """按目标配置执行一次探测。"""
    if target.kind == "tcp":
        return probe_tcp(target.host, target.port or 443, target.timeout_ms)
    return probe_icmp(target.host, target.timeout_ms)


# ---------------------------------------------------------------- 环境探测

def detect_gateway() -> str | None:
    """找出本机 IPv4 默认网关（局域网基准，用于区分本地链路问题和外网问题）。

    优先解析路由表：ipconfig 在只有 IPv6 网关的网卡上拿不到 IPv4 网关。
    """
    gw = _gateway_from_route_table()
    if gw:
        return gw
    return _gateway_from_ipconfig()


def _gateway_from_route_table() -> str | None:
    try:
        cp = subprocess.run(
            ["route", "print", "-4"], capture_output=True, timeout=8,
            creationflags=_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    ip_re = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
    best: tuple[int, str] | None = None
    for line in _decode(cp.stdout).splitlines():
        parts = line.split()
        # 网络目标  掩码  网关  接口  度量
        if len(parts) < 5 or parts[0] != "0.0.0.0" or parts[1] != "0.0.0.0":
            continue
        gw_ip = parts[2]
        if not ip_re.match(gw_ip) or gw_ip == "0.0.0.0":
            continue
        try:
            metric = int(parts[-1])
        except ValueError:
            metric = 9999
        if best is None or metric < best[0]:
            best = (metric, gw_ip)
    return best[1] if best else None


def _gateway_from_ipconfig() -> str | None:
    try:
        cp = subprocess.run(
            ["ipconfig"], capture_output=True, timeout=8, creationflags=_NO_WINDOW
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    for line in _decode(cp.stdout).splitlines():
        if "网关" in line or "gateway" in line.lower():
            for ip in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", line):
                if ip not in ("0.0.0.0", "255.255.255.255"):
                    return ip
    return None


def local_ip() -> str | None:
    """通过一次 UDP connect 拿到本机在默认出口网卡上的 IPv4。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def traceroute(host: str, max_hops: int = 15, timeout_ms: int = 800, max_wait: float = 45.0):
    """调用系统 tracert，返回 (跳数, IP, 耗时) 列表。阻塞调用，请放后台线程。"""
    cmd = ["tracert", "-d", "-h", str(max_hops), "-w", str(timeout_ms), host]
    try:
        cp = subprocess.run(
            cmd, capture_output=True, timeout=max_wait, creationflags=_NO_WINDOW
        )
    except subprocess.TimeoutExpired:
        return [], "tracert 执行超时"
    except (FileNotFoundError, OSError) as exc:
        return [], f"无法执行 tracert: {exc}"

    text = _decode(cp.stdout)
    hops: list[tuple[int, str, str]] = []
    line_re = re.compile(r"^\s*(\d+)\s+(.*)$")
    ip_re = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    for line in text.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        idx = int(m.group(1))
        rest = m.group(2)
        ips = ip_re.findall(rest)
        times = re.findall(r"(\d+)\s*ms", rest)
        if ips:
            hops.append((idx, ips[-1], f"{times[-1]} ms" if times else "-"))
        else:
            hops.append((idx, "*", "超时"))
    return hops, ""
