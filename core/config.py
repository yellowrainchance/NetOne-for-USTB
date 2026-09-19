"""统一配置：延迟监测目标 + 校园网额度参数，共用一份 config.json。

合并前是两个项目各自一份配置（NetPing 的 core/config.py 管目标与探测，
NetQuota 的 core/config.py 管账号与额度）。这里合成一个 Config：

    cfg.settings   —— 探测/悬浮窗/窗口（原 NetPing 的 settings 字典）
    cfg.targets    —— 延迟监测目标列表
    cfg.campus     —— 校园网额度参数（原 NetQuota 的扁平字段，收进 CampusConfig）

数据目录与程序放一起（绿色便携），测试可用环境变量 NETONE_HOME 覆盖。
"""
from __future__ import annotations

import json
import os
import sys
import tarfile
import time
import uuid
from dataclasses import asdict, dataclass, field

APP_NAME = "NetOne"

# 顶层窗口标题 —— 必须只有这一处定义。
# 单实例检测靠标题认"这是不是自己的窗口"，而标题同时也是用户能看到的东西，
# 两者必须同源。曾经的写法是拿 startswith("NetOne") 去匹配，结果资源管理器
# 打开 netone 目录时窗口标题正好是「NetOne - 文件资源管理器」，
# 被当成"已有实例在跑"→ exe 静默退出 → 用户看到的就是"双击没反应、启动不了"。
MAIN_TITLE = f"{APP_NAME} · 网络延迟与校园网流量"
OVERLAY_TITLE = f"{APP_NAME} 悬浮窗"

# 曲线配色（浅色背景上对比度足够）
PALETTE = [
    "#2f6fed",  # 蓝
    "#e8503a",  # 红
    "#12a150",  # 绿
    "#a855f7",  # 紫
    "#f59e0b",  # 橙
    "#0891b2",  # 青
    "#db2777",  # 玫红
]


# ---------------------------------------------------------------- 监测目标


@dataclass
class Target:
    """一个被监测的延迟目标（域名/IP + 端口）。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = "新目标"
    host: str = ""
    port: int = 443
    kind: str = "tcp"          # tcp | icmp
    enabled: bool = True
    interval: float = 1.0      # 秒
    timeout_ms: int = 1500
    color: str = PALETTE[0]
    role: str = "target"       # target（关注目标） | gateway（本地网关） | baseline（公网基准）
    builtin: bool = False      # 内置目标不允许改成别的用途，但可以禁用

    @property
    def addr(self) -> str:
        return f"{self.host}:{self.port}" if self.kind == "tcp" else self.host

    @property
    def kind_label(self) -> str:
        return "TCP" if self.kind == "tcp" else "ICMP"


def default_targets(gateway: str | None) -> list[Target]:
    """默认监测组：你关心的目标 + 国内基准 + 境外基准 +（可选）本地网关。

    同时监测网关、国内基准和境外基准，是为了区分
    "是我本地网络卡" / "是国内线路卡" / "是出境线路卡" / "是对方服务器卡"。
    """
    out: list[Target] = [
        Target(name="EA 官网", host="www.ea.com", port=443, kind="tcp",
               color=PALETTE[0], role="target", builtin=True),
        Target(name="百度", host="www.baidu.com", port=443, kind="tcp",
               color=PALETTE[1], role="target", builtin=True),
        # 国内基准：阿里公共 DNS，基本任何国内网络都通
        Target(name="国内基准", host="223.5.5.5", kind="icmp",
               color=PALETTE[5], role="baseline", builtin=True),
        # 境外基准：Cloudflare。用来判断出境线路（8.8.8.8 在国内常被墙，不适合做基准）
        Target(name="境外基准", host="1.1.1.1", kind="icmp",
               color=PALETTE[3], role="baseline", builtin=True),
    ]
    if gateway:
        out.append(_gateway_target(gateway))
    return out


def _gateway_target(gateway: str) -> Target:
    """网关目标：先试 ICMP，被屏蔽就退而用 TCP 80（路由器管理页）。

    校园网和不少路由器都会屏蔽 ICMP，直接加进去会得到一个永远"超时"的目标，
    反而干扰判断，所以这里探一次，能测通才用。
    """
    from core.net.probe import probe_icmp, probe_tcp

    if probe_icmp(gateway, 1200).ok:
        return Target(name="本地网关", host=gateway, kind="icmp",
                      color=PALETTE[2], role="gateway", builtin=True)
    for port in (80, 443):
        if probe_tcp(gateway, port, 1200).ok:
            return Target(name="本地网关", host=gateway, port=port, kind="tcp",
                          color=PALETTE[2], role="gateway", builtin=True)
    return Target(name="本地网关", host=gateway, kind="icmp",
                  color=PALETTE[2], role="gateway", builtin=True, enabled=False)


# ---------------------------------------------------------------- 校园网额度


@dataclass
class CampusConfig:
    """北科大校园网额度监测的全部参数（原 NetQuota 的 Config）。"""

    enabled: bool = True

    account: str = ""                       # 选填；留空自动跟随当前认证会话
    suffix: str = ""                        # 运营商后缀 @dx / @lt，一般留空

    portal_url: str = "http://202.204.48.66/"
    eportal_host: str = "202.204.48.66"
    eportal_port: int = 801
    timeout: float = 6.0
    poll_interval: int = 60                 # 秒，不建议低于 30

    # 计费规则
    quota_gb: float = 120.0
    gb_base: int = 1024                     # 1 GB = 1024 MB（官方未明确，可改 1000）
    price_per_gb: float = 0.6

    # 额度口径：v4 = ePortal loadUserFlow 的 v4（≈自助服务权威账本，主指标）
    #           flow = 门户首页内嵌变量（滞后/次级口径，仅作对照）
    quota_source: str = "v4"

    # 提醒阈值
    warn_left_gb: float = 20.0
    critical_left_gb: float = 5.0
    warn_balance_yuan: float = 3.0
    surge_factor: float = 2.0               # 当日用量 > 近 7 日均值 x 该倍数 -> 突增提醒
    max_devices: int = 4
    quiet_hours: str = "23:00-08:00"
    notify_cooldown: int = 30               # 分钟

    # 通知开关（总开关 + 各类别）
    notify_enabled: bool = True
    notify_quota: bool = True
    notify_surge: bool = True
    notify_forecast: bool = True
    notify_balance: bool = True
    notify_device: bool = True

    # 设备
    device_check: bool = True
    device_interval: int = 300              # 秒

    def full_account(self) -> str:
        """带运营商后缀的完整账号。"""
        return f"{self.account}{self.suffix}" if self.suffix else self.account

    def device_account(self, session_account: str = "") -> str:
        """设备管理所用账号：设置的固定账号优先，留空时自动跟随会话账号。

        账号是选填的——显示流量不需要账号（门户首页按当前会话返回），
        只有设备管理需要一个账号。为了不逼用户手动填写：
          1. 设置了固定账号 → 用它，可离线查询/多账号；
          2. 留空 → 自动跟随「当前在线会话账号」，开箱即用。
        """
        if (self.account or "").strip():
            return self.full_account()
        return (session_account or "").strip()


# ---------------------------------------------------------------- 默认设置

DEFAULT_SETTINGS = {
    "interval": 1.0,
    "timeout_ms": 1500,
    "overlay": {
        "x": None,
        "y": None,
        "w": 300,
        "compact": False,
        "opacity": 0.86,
        "locked": False,
        "visible": True,
        "show_quota": True,      # 悬浮窗顶部显示额度环 + 剩余 GB
        "show_used": True,       # 额度区副行显示已用流量
        "show_balance": True,    # 额度区副行显示账户余额
        "show_ping": True,       # 单行显示当前延迟
        "ping_tid": "",          # 「当前延迟」取哪个目标；空 = 第一个启用的目标
        "show_latency": True,    # 悬浮窗显示各目标延迟行
    },
    # 延迟（Ping）监测总开关：**默认关闭**，用户手动打开后持久化。
    # 校园网额度监测不受它影响，程序一进去就是可用的校园网检测。
    "latency_enabled": False,
    # h=800：主窗口的最小高度由布局真实需要决定（约 780），默认值必须不小于它，
    # 否则程序一启动就是在"布局被压到重叠"的状态下显示（见 MainWindow._pin_min_height）
    "window": {"w": 1180, "h": 800},
    "chart_window": 300,         # 延迟曲线默认显示最近多少秒
    "last_tab": "campus",        # 启动落在哪一页：campus（校园网优先） / latency
    "autostart": False,
    "close_to_tray": True,
    "start_to_tray": False,
}

# 图表可选的时间范围：(显示名, 秒数)。None = 全部数据。
CHART_WINDOWS: list[tuple[str, float | None]] = [
    ("最近 1 分钟", 60),
    ("最近 5 分钟", 300),
    ("最近 15 分钟", 900),
    ("全部数据", None),
]

OVERLAY_W_MIN = 200
OVERLAY_W_MAX = 560

# 额度查询的可选间隔（秒）
QUOTA_INTERVALS = [30, 60, 120, 300]


# ---------------------------------------------------------------- 目录

# 数据统一收进「运行目录/data」子文件夹（用户要求）：config.json、netone.db、
# netone.log、latency_history.json、归档 tar 都在这里，exe 旁只留程序本体。
DATA_SUBDIR = "data"

# 旧布局（数据散在运行目录根）时代用过的文件名。升级迁移时按这个清单认领。
LEGACY_NAMES = (
    "config.json",
    "netone.db",
    "netone.db-wal",
    "netone.db-shm",
    "latency_history.json",
    "netone.log",
)

# 日志轮转：netone.log 超过这个大小就压成 tar 归档，目录里永远只留最新一份
# 可直接用记事本打开的 TXT（用户要求：查错误只看 latest）。
LOG_NAME = "netone.log"
LOG_ROTATE_BYTES = 1 * 1024 * 1024
LOG_KEEP = 5


def is_frozen() -> bool:
    """是否处于 PyInstaller 打包运行态。"""
    return bool(getattr(sys, "frozen", False))


def base_dir() -> str:
    """运行目录（不含 data 子目录）：源码运行 = 项目根；打包后 = exe 所在目录。

    环境变量 NETONE_HOME 覆盖的是**运行目录**（不是数据目录本身），
    与默认布局保持同一语义：数据一律在其下的 data/ 里。测试用它隔离。
    """
    override = os.environ.get("NETONE_HOME")
    if override:
        return override
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir() -> str:
    """数据目录 = 运行目录/data。

    整个 data 文件夹拷走即完成迁移。运行目录不可写（比如装在 Program Files）
    时退回 %APPDATA%\\NetOne\\data。测试用 NETONE_HOME 覆盖。
    """
    here = base_dir()
    if os.access(here, os.W_OK):
        path = os.path.join(here, DATA_SUBDIR)
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            pass

    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME, DATA_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def migrate_legacy_data() -> None:
    """旧布局升级迁移（幂等）：运行目录根的散落数据文件 → data/。

    1. 旧文件全部打包成 data/legacy-<时间戳>.tar.gz 存档（tar 失败不阻塞迁移）；
    2. 活跃文件（config.json / netone.db* / latency_history.json / netone.log）
       移动进 data/ 继续使用 —— 配置、额度历史、延迟历史一样不丢；
    3. data/config.json 已存在视为迁移过（或用户手动搬过），一律不再动，
       避免旧文件覆盖新数据。

    必须在 Config.load() 之前调用：否则旧 config.json 还在根目录，新代码会
    找不到它，把用户当成首运行重新生成一套默认配置。
    """
    base = base_dir()
    legacy = [n for n in LEGACY_NAMES if os.path.exists(os.path.join(base, n))]
    if not legacy:
        return

    try:
        new = os.path.join(base, DATA_SUBDIR)
        os.makedirs(new, exist_ok=True)
    except OSError:
        return
    if os.path.exists(os.path.join(new, "config.json")):
        return

    archive = os.path.join(new, f"legacy-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    try:
        with tarfile.open(archive, "w:gz") as tf:
            for name in legacy:
                try:
                    tf.add(os.path.join(base, name), arcname=name)
                except OSError:
                    pass
    except (OSError, tarfile.TarError):
        pass    # 存档是锦上添花，失败也要继续把活跃文件迁过去

    for name in legacy:
        try:
            os.replace(os.path.join(base, name), os.path.join(new, name))
        except OSError:
            pass    # 文件被占用（比如双开的旧实例）时留在原地，不让程序起不来


def rotate_log_if_needed() -> None:
    """日志轮转：netone.log 超过 LOG_ROTATE_BYTES 就压成 tar 并删原件。

    轮转后 data/ 里重新出现空的 netone.log —— 目录里永远只有一份可直接
    打开阅读的 latest TXT，旧日志都在 data/logs/*.tar.gz 里（最多 LOG_KEEP 份）。
    """
    path = os.path.join(data_dir(), LOG_NAME)
    try:
        if not os.path.exists(path) or os.path.getsize(path) < LOG_ROTATE_BYTES:
            return
        logs = os.path.join(data_dir(), "logs")
        os.makedirs(logs, exist_ok=True)
        archive = os.path.join(logs, f"netone-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(path, arcname=LOG_NAME)
        os.remove(path)
        olds = sorted(f for f in os.listdir(logs) if f.startswith("netone-"))
        for name in olds[:-LOG_KEEP]:
            try:
                os.remove(os.path.join(logs, name))
            except OSError:
                pass
    except OSError:
        pass    # 日志归档失败不影响任何主流程


def config_path() -> str:
    return os.path.join(data_dir(), "config.json")


# ---------------------------------------------------------------- Config


class Config:
    """整个程序的配置。"""

    def __init__(self):
        self.settings: dict = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.targets: list[Target] = []
        self.campus = CampusConfig()
        # load() 里"凭空补出来"的内容（默认目标、默认设置）标记一下，
        # 由 app 在启动时落一次盘。否则 config.json 会和程序实际在用的值不一致：
        # 文件里 targets 是空的，程序里却有 5 个目标 —— 用户打开文件想看目标
        # 只会一脸问号，而且不触发任何保存动作就永远对不上。
        self.regenerated = False
        self.load()

    # ---------------------------------------------------------- 读写

    def load(self) -> None:
        path = config_path()
        raw: dict = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, json.JSONDecodeError):
                raw = {}
        else:
            self.regenerated = True      # 首次运行：文件都没有

        settings = raw.get("settings") or {}
        # 只认 DEFAULT_SETTINGS 里列过的键。
        # 坑：在运行期往 settings 里塞的自定义键（比如 last_tab），如果没先在
        # DEFAULT_SETTINGS 登记，save() 会写进文件、load() 却静默丢掉 ——
        # 表现为"设置里改了、重启就复原"，而且文件里明明写着。
        for key, val in DEFAULT_SETTINGS.items():
            got = settings.get(key)
            if isinstance(val, dict):
                merged = dict(val)
                if isinstance(got, dict):
                    merged.update(got)
                self.settings[key] = merged
            elif got is not None:
                self.settings[key] = got

        campus = raw.get("campus") or {}
        allowed = {k: v for k, v in campus.items()
                   if k in CampusConfig.__dataclass_fields__}
        try:
            self.campus = CampusConfig(**{**asdict(self.campus), **allowed})
        except TypeError:
            self.campus = CampusConfig()

        targets: list[Target] = []
        for item in raw.get("targets") or []:
            if not isinstance(item, dict) or not item.get("host"):
                continue
            allow = {k: v for k, v in item.items() if k in Target.__dataclass_fields__}
            try:
                targets.append(Target(**allow))
            except TypeError:
                continue

        if not targets:
            from core.net.probe import detect_gateway

            targets = default_targets(detect_gateway())
            # 文件里没有目标（首次运行，或用户把 targets 清空了）→ 补默认值，
            # 并让 app 启动时落盘，保证文件内容 = 程序实际在用的内容
            self.regenerated = True
        self.targets = targets

    def save(self) -> None:
        data = {
            "settings": self.settings,
            "targets": [asdict(t) for t in self.targets],
            "campus": asdict(self.campus),
        }
        path = config_path()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass

    # ---------------------------------------------------------- 便捷访问

    @property
    def interval(self) -> float:
        return float(self.settings.get("interval", 1.0))

    @property
    def timeout_ms(self) -> int:
        return int(self.settings.get("timeout_ms", 1500))

    @property
    def overlay_cfg(self) -> dict:
        return self.settings.setdefault("overlay", dict(DEFAULT_SETTINGS["overlay"]))

    def next_color(self) -> str:
        used = {t.color for t in self.targets}
        for c in PALETTE:
            if c not in used:
                return c
        return PALETTE[len(self.targets) % len(PALETTE)]

    def enabled_targets(self) -> list[Target]:
        return [t for t in self.targets if t.enabled and t.host]

    def apply_probe_defaults(self, interval: float, timeout_ms: int) -> None:
        """把"统一探测间隔/超时"写到所有目标上。

        设置里改的是整体值 —— 目标一多，逐个去编辑对话框改太折磨人。
        单个目标仍然可以在编辑对话框里单独覆盖（下次改整体值会再被覆盖回来）。
        """
        self.settings["interval"] = float(interval)
        self.settings["timeout_ms"] = int(timeout_ms)
        for target in self.targets:
            target.interval = float(interval)
            target.timeout_ms = int(timeout_ms)

    def active_intervals(self) -> list[float]:
        """启用中目标的探测间隔（去重排序），用来如实播报"每 X 秒一次"。"""
        return sorted({t.interval for t in self.targets if t.enabled and t.host})

    def quota(self):
        """按当前计费规则构造一个 Quota（延迟导入避免层次倒置）。"""
        from core.campus.models import Quota

        return Quota(self.campus.quota_gb, self.campus.gb_base,
                     self.campus.price_per_gb)
