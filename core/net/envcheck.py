"""运行环境检查：当前测到的延迟是"直连"还是"经代理/加速器"。

定位很重要：开着加速器时测出来的延迟，就是玩家在游戏里实际体验到的延迟 ——
这是个**有效读数**，不是需要排除的脏数据。这个模块的作用是给读数贴上正确的标签，
并在用户中途开关加速器时把变化记进事件日志，好让他直接对比"开之前/开之后"。

检测三件事：
1. 系统代理（注册表 ProxyEnable / AutoConfigURL）
2. 默认路由是否走隧道网卡（TUN/BUN/虚拟网卡）
3. 有没有代理类程序在跑（含游戏加速器）

成本差别很大，所以分两档：
  轻量检查（注册表 + route print，约 190ms）—— 适合后台定期复查
  完整检查（再加 tasklist，约 800ms）—— 启动时和按需时跑
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

from .probe import IS_WINDOWS, _NO_WINDOW, _decode

# 网卡名里出现这些词的，基本就是隧道/虚拟网卡
TUNNEL_HINTS = (
    "warp", "wireguard", "tap-", "tun", "clash", "mihomo", "v2ray", "xray",
    "sing-box", "openvpn", "vpn", "shadow", "ssr", "trojan", "tailscale", "zerotier",
    "加速器",
)

# 代理/VPN/游戏加速器的进程名。
# 分两级：_STRONG 够独特，直接当子串匹配；_TOKENS 太短容易误伤
# （例如 'ssr' 会命中 Windows Defender 的 NisSrv.exe），要求作为独立词或词首出现。
_STRONG = (
    # 代理 / VPN
    "clash", "mihomo", "v2ray", "xray", "singbox", "sing-box", "trojan",
    "wireguard", "openvpn", "tailscale", "zerotier", "hysteria", "proxifier",
    "sstap", "shadowsocks", "naive", "netch",
    # 游戏加速器
    "uugameboost", "uubooster", "uugame", "xunyou", "leigod", "qiyou",
    "biubiu", "teniodl", "gameaccelerator", "gameaccel", "netgameboost",
)
_TOKENS = ("warp", "vpn", "proxy", "surge", "ssr", "ss-local", "accel", "booster", "加速")

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def is_proxy_process(name: str) -> bool:
    stem = name.rsplit(".", 1)[0] if name.lower().endswith(".exe") else name
    low = stem.lower()
    flat = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", low)
    if any(k in flat for k in _STRONG):
        return True
    tokens = [t for t in re.split(r"[^a-z0-9\u4e00-\u9fff]+", low) if t]
    return any(t == k or t.startswith(k) for t in tokens for k in _TOKENS)


@dataclass
class EnvReport:
    # (good|info|warn, 标题, 说明) —— 与 Engine.diagnose_with() 的结论同构
    notes: list[tuple[str, str, str]] = field(default_factory=list)
    system_proxy: str = ""
    tunnel_adapters: list[str] = field(default_factory=list)
    proxy_procs: list[str] = field(default_factory=list)
    tunneled: bool = False
    # direct = 直连；proxied = 流量经代理/加速器
    mode: str = "direct"

    @property
    def proxied(self) -> bool:
        return self.mode == "proxied"

    @property
    def chip(self) -> str:
        """主界面上那个小标签。

        进程名在这里截断：它是用户可控的字符串（进程名），
        不截断的话一个超长名字会把状态条撑宽，把窗口的最小宽度顶上去。
        完整的名字在 chip_tip 里。
        """
        if self.proxied:
            who = self.proxy_procs[0].rsplit(".", 1)[0] if self.proxy_procs else "代理"
            if len(who) > 14:
                who = who[:13] + "…"
            return f"经代理 · {who}"
        return "直连"

    @property
    def chip_tip(self) -> str:
        if self.proxied:
            bits = []
            if self.system_proxy:
                bits.append(f"系统代理：{self.system_proxy}")
            if self.tunneled:
                bits.append("默认路由走隧道网卡：" + "、".join(self.tunnel_adapters))
            if self.proxy_procs:
                bits.append("在跑的程序：" + "、".join(self.proxy_procs[:4]))
            return "当前延迟是经代理/加速器转发后的结果（也就是游戏里的实际体验值）。\n" + "\n".join(bits)
        return "流量直连，没有检测到代理或 VPN 接管。"


def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout,
                            creationflags=_NO_WINDOW)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
    return _decode(cp.stdout)


def parse_adapters(text: str) -> list[tuple[str, list[str]]]:
    """`ipconfig` 输出 -> [(网卡名, IPv4 列表)]。

    返回**所有**网卡（已断开的那个列表是空的），由调用方决定要不要过滤。

    两个细节（按本机真实输出核对过）：
    - 网卡名那一行顶格、以冒号结尾（"无线局域网适配器 WLAN:"），
      而下面的属性行是缩进的 —— 拿这个区分网卡边界。
    - 只认含 "IPv4" / "IP Address" 的行。**不能**顺手收所有 IP：
      "默认网关"那行也带 IPv4 地址，而且子网掩码那行还是 255.x.x.x，
      收进来会把网关当成网卡自己的地址。
    """
    out: list[tuple[str, list[str]]] = []
    name = ""
    ips: list[str] = []
    for line in text.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            if name:
                out.append((name, ips))
            name, ips = line.strip().rstrip(":"), []
            continue
        if "IPv4" in line or "IP Address" in line:
            for ip in _IPV4.findall(line):
                if ip not in ("0.0.0.0", "255.255.255.255"):
                    ips.append(ip)
    if name:
        out.append((name, ips))
    return out


def parse_default_route(text: str) -> str:
    """`route print -4` 输出 -> 默认路由的出口接口 IP。

    形如 `0.0.0.0  0.0.0.0  <网关>  <接口IP>  <跃点数>`，第 4 列是**接口**而不是网关。
    多条默认路由时取跃点数最小的那条 —— 开 TUN 模式时正是这种情况：
    物理网卡那条还在，但代理给自己那条的跃点数更低。
    """
    best: tuple[int, str] | None = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "0.0.0.0" or parts[1] != "0.0.0.0":
            continue
        try:
            metric = int(parts[-1])
        except ValueError:
            metric = 9999
        iface = parts[3]
        if best is None or metric < best[0]:
            best = (metric, iface)
    return best[1] if best else ""


def is_tunnel_name(name: str) -> bool:
    """网卡名里带隧道/代理/加速器关键词。"""
    return any(h in name.lower() for h in TUNNEL_HINTS)


def tunnel_hit(iface: str, adapters: list[tuple[str, list[str]]]) -> str | None:
    """默认路由的出口 IP 落在哪块隧道网卡上？返回网卡名，没落上就 None。

    按"IP 属于哪块网卡"来判，而不是拿 IP 去字符串匹配拼接后的显示文本 ——
    后者在 IP 互为子串时会误判。
    """
    if not iface:
        return None
    for name, ips in adapters:
        if iface in ips and is_tunnel_name(name):
            return name
    return None


def _read_registry_proxy() -> str:
    if not IS_WINDOWS:
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
    except OSError:
        return ""
    out = []
    try:
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if enabled:
            try:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except (FileNotFoundError, OSError):
                server = "已启用"
            out.append(f"固定代理 {server}")
    except (FileNotFoundError, OSError):
        pass
    try:
        pac, _ = winreg.QueryValueEx(key, "AutoConfigURL")
        if pac:
            out.append(f"PAC 脚本 {pac}")
    except (FileNotFoundError, OSError):
        pass
    return "；".join(out)


def _running_proxy_procs() -> list[str]:
    try:
        from .conns import process_names
    except ImportError:
        return []
    return sorted({n for n in process_names().values() if is_proxy_process(n)})


def build_notes(report: EnvReport) -> list[tuple[str, str, str]]:
    """把检测结果翻成人话：[(级别, 标题, 说明)]。

    纯函数，便于测试措辞和级别；返回结构和 Engine.diagnose_with() 的结论一致，
    这样诊断对话框可以直接把两边的结论拼在一起显示。

    关键立场：开着加速器测到的延迟是**有效读数**（就是玩家游戏里体验到的那个值），
    所以这种情况是 info，不是 warn —— 不要在措辞里暗示用户"数据不可信、去关掉它"。
    """
    if report.proxied:
        who = "、".join(filter(None, [
            report.proxy_procs[0] if report.proxy_procs else "",
            report.system_proxy,
            ("隧道网卡 " + report.tunnel_adapters[0]) if report.tunneled else "",
        ])) or "代理"
        return [(
            "info",
            "流量正走代理/加速器 —— 这个延迟就是游戏里的实际体验值",
            f"当前由 {who} 接管（{report.chip}）。这是有效读数，不用把它关掉。"
            "不过代理通常只接管境外流量：国内目标往往还是直连的几十毫秒，"
            "境外目标则变成了代理节点的延迟 —— 两个数字含义不同，别混在一起比。"
            "想知道代理有没有帮上忙，看事件日志里开关那两条记录的前后对比。",
        )]
    if report.proxy_procs:
        shown = "、".join(report.proxy_procs[:3])
        if len(report.proxy_procs) > 3:
            shown += f" 等 {len(report.proxy_procs)} 个"
        return [(
            "info",
            "有代理/加速器程序在跑，但当前没有接管流量",
            f"检测到 {shown}。所以现在测的是直连延迟。"
            "你打开它的加速开关后，这个标签会自动跟着变，并往事件日志里记一条，"
            "方便你直接对比开关前后的差别。",
        )]
    return [(
        "good",
        "流量直连",
        "没有检测到代理或 VPN 接管，测到的就是线路本身的延迟。",
    )]


def inspect(full: bool = True) -> EnvReport:
    """检查当前环境。

    full=False 只跳过 tasklist（它最慢，约 600ms，且只影响"在跑哪些代理程序"这个展示项）。
    判断流量是否被接管靠注册表 + 路由表 + ipconfig，所以轻量模式一样准。
    """
    report = EnvReport()
    report.system_proxy = _read_registry_proxy()

    adapters = parse_adapters(_run(["ipconfig"]))
    iface = parse_default_route(_run(["route", "print", "-4"]))

    # 装了隧道网卡 ≠ 流量走了隧道：得看默认路由是不是从那块网卡出去的
    report.tunnel_adapters = [
        f"{name} ({', '.join(ips)})"
        for name, ips in adapters
        if ips and is_tunnel_name(name)
    ]
    hit = tunnel_hit(iface, adapters)
    report.tunneled = hit is not None

    if full:
        report.proxy_procs = _running_proxy_procs()

    report.mode = "proxied" if (report.system_proxy or report.tunneled) else "direct"
    report.notes = build_notes(report)
    return report
