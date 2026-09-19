"""数据模型定义。

## 额度口径（本版与 NetQuota 第一代的关键差异）

第一代用门户首页内嵌变量 `flow`（KB）当计费流量。实测与文档分析表明
`flow` 是**滞后/次级口径**：同刻采样下它比自助服务权威账本少 1.2~1.6 GB，
而 ePortal 的 `visitor/loadUserFlow` 返回的 **`v4` 与权威账本口径一致**。

所以本版把 **`v4` 作为主指标**（`Snapshot.used_mb`），门户 `flow` 只留作对照
（`Snapshot.flow_kb`），界面上小字标出差值，方便自己核对。

    used_mb  = ePortal loadUserFlow 的 v4（MB）—— 扣 120G 额度看这个
    flow_kb  = 门户首页 flow（KB）—— 滞后口径，仅对照
    v6_mb    = IPv6 流量（MB）—— 完全免费，不计入额度

接口取不到时自动退回 `flow` 兜底（`Snapshot.source == "portal"`），
界面上会标明"门户口径"，不会假装是权威值。

### 2026-09-19 实测补充（重要，别被"少 1.2~1.6 GB"误导）

当天 11:5x 与 12:1x 两次同时采样，两个口径**数值完全相等**
（v4 = 84829.03 MB，门户 flow = 86864930 KB = 84829.03 MB，差值 -0.000 GB）。
也就是说那个 1.2~1.6 GB 的差值不是恒定的，会随校内侧流量占比变化，
某些时段两个口径会重合。

对程序的影响：不影响本版选择。`v4` 仍是主指标，且实测中它**从不小于** `flow`
（校内侧资源只可能让接口偏大），所以拿它扣额度是偏保守的一侧 ——
不会出现"界面说还剩很多、其实已经超额"的情况。
`Snapshot.gap_gb` 就是给用户自己看这个差值用的，为 0 时说明两口径当时重合。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

KB_PER_MB = 1024
KB_PER_GB = 1024 * 1024
MB_PER_GB = 1024

# 接口取不到时 source 用这个值，界面据此提示"这是门户口径"
SOURCE_API = "api"        # ePortal loadUserFlow v4 —— 主口径
SOURCE_PORTAL = "portal"  # 门户首页 flow —— 兜底口径


@dataclass
class Snapshot:
    """一次轮询得到的账号状态快照。"""

    ts: float = field(default_factory=time.time)
    account: str = ""
    online: bool = False
    source: str = ""         # SOURCE_API / SOURCE_PORTAL / ""（无数据）

    used_mb: float = 0.0     # ⭐ 主指标：已用额度（MB），口径见模块注释
    flow_kb: int = 0         # 门户首页 flow（KB），滞后口径，仅对照
    v6_mb: float = 0.0       # IPv6 流量 MB（免费，不计额度）
    balance_raw: int = 0     # 账户余额 x10000（页面变量 fee）
    minutes: int = 0         # 已用时长 分钟（页面变量 time）

    v4ip: str = ""
    v6ip: str = ""
    error: str = ""

    # ---------- 派生量 ----------
    @property
    def used_gb(self) -> float:
        return self.used_mb / MB_PER_GB

    @property
    def flow_gb(self) -> float:
        return self.flow_kb / KB_PER_GB

    @property
    def v6_gb(self) -> float:
        return self.v6_mb / MB_PER_GB

    @property
    def balance_yuan(self) -> float:
        return self.balance_raw / 10000

    @property
    def gap_gb(self) -> float:
        """主指标比门户口径多算了多少 GB（≈ 校内免费资源那部分）。"""
        return self.used_gb - self.flow_gb

    @property
    def source_label(self) -> str:
        if self.source == SOURCE_API:
            return "接口口径"
        if self.source == SOURCE_PORTAL:
            return "门户口径（接口未取到）"
        return "无数据"


@dataclass
class Quota:
    """计费套餐参数（对应官方规则）。"""

    quota_gb: float = 120.0        # 每月免费额度
    gb_base: int = 1024            # 1 GB = 1024 MB（官方未明确，做成配置项）
    price_per_gb: float = 0.6      # 超额单价 元/GB

    @property
    def quota_mb(self) -> float:
        return self.quota_gb * self.gb_base

    def left_gb(self, snap: Snapshot) -> float:
        """本月剩余额度（GB）。按主指标 used_mb 计算。"""
        return self.quota_gb - snap.used_gb

    def used_percent(self, snap: Snapshot) -> float:
        if self.quota_gb <= 0:
            return 0.0
        return min(100.0, max(0.0, snap.used_gb / self.quota_gb * 100))

    def left_percent(self, snap: Snapshot) -> float:
        """剩余百分比（环形进度用）。超额后为 0，不会画成负数。"""
        return max(0.0, 100.0 - self.used_percent(snap))

    def over_fee(self, used_gb: float) -> float:
        """给定用量，计算超额费用（未超额为 0）。"""
        return max(0.0, used_gb - self.quota_gb) * self.price_per_gb

    def buyable_gb(self, snap: Snapshot) -> float:
        """当前余额还能购买多少 GB 超额流量。"""
        if self.price_per_gb <= 0:
            return 0.0
        return snap.balance_yuan / self.price_per_gb


@dataclass
class Device:
    """一个在线终端。"""

    ip: str = ""
    mac: str = ""
    host: str = ""
    online_time: str = ""
    time_long: int = 0          # 在线时长（秒）
    downlink_bytes: int = 0
    uplink_bytes: int = 0
    is_local: bool = False

    alias: str = ""         # 用户自定义名称（来自 device_alias 表）
    trusted: bool = False   # 已信任，不再作为陌生设备提醒

    @property
    def display_name(self) -> str:
        """界面上显示的名字：别名 > 主机名 > IP。"""
        return self.alias or self.host or self.ip or "未知设备"

    @property
    def mac_display(self) -> str:
        """格式化为 XX:XX:XX:XX:XX:XX"""
        m = self.mac.replace(":", "").replace("-", "").strip()
        if len(m) != 12:
            return self.mac.upper()
        return ":".join(m[i:i + 2] for i in range(0, 12, 2)).upper()

    @property
    def downlink_gb(self) -> float:
        return self.downlink_bytes / (1024 ** 3)

    @property
    def time_long_text(self) -> str:
        h, rem = divmod(self.time_long, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h} 小时 {m} 分"
        if m:
            return f"{m} 分 {s} 秒"
        return f"{s} 秒"
