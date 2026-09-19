"""用量统计：日用量、日预算、突增检测、月底预测。

所有数据源来自 Store 的 daily 表，不直接依赖网络。
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from .models import Quota, Snapshot
from .store import Store

MIN_SURGE_GB = 0.5      # 低于此值不做突增判定，避免小样本误报
FALLBACK_DAYS = 3       # 本月已过天数少于此值时，改用近期日均外推


class Stats:
    """用量统计。每次数据采集后调用 refresh()。"""

    def __init__(self, store: Store, quota: Quota, surge_factor: float = 2.0):
        self.store = store
        self.quota = quota
        self.surge_factor = surge_factor
        self._rows: list[dict] = []

    def refresh(self) -> None:
        self._rows = self.store.daily_rows(limit=45)

    # ------------------------------------------------------------ 基础
    @property
    def rows(self) -> list[dict]:
        return self._rows

    def today_used_gb(self) -> float:
        today = date.today().strftime("%Y-%m-%d")
        for r in self._rows:
            if r["day"] == today:
                return r["used_gb"]
        return 0.0

    def daily_budget_gb(self) -> float:
        """把月额度平摊到每天。"""
        now = datetime.now()
        days = calendar.monthrange(now.year, now.month)[1]
        return self.quota.quota_gb / days

    def avg_gb(self, days: int = 7) -> float:
        """最近 N 天（含今天）的日均用量。"""
        if not self._rows:
            return 0.0
        recent = [r["used_gb"] for r in self._rows[-days:]]
        if not recent:
            return 0.0
        return sum(recent) / len(recent)

    # ------------------------------------------------------------ 判定
    def surge(self) -> tuple[bool, float, float]:
        """是否突增。返回 (是否突增, 今日用量, 近 7 日均值)。"""
        today = self.today_used_gb()
        baseline = self.avg_gb(7)
        if today < MIN_SURGE_GB or baseline <= 0:
            return False, today, baseline
        return today > baseline * self.surge_factor, today, baseline

    def forecast_gb(self, snap: Snapshot | None = None) -> float:
        """预测本月底总用量。"""
        now = datetime.now()
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        elapsed = (now.day - 1) + now.hour / 24 + now.minute / 1440

        if snap is not None and elapsed >= FALLBACK_DAYS:
            return snap.used_gb / elapsed * days_in_month
        # 月初样本太少，改用近期日均外推
        return self.avg_gb(7) * days_in_month

    # ------------------------------------------------------------ 曲线
    def history(self, days: int = 30) -> list[dict]:
        """近 N 天日用量，缺失日期补 0，保证曲线连续。"""
        mapping = {r["day"]: r["used_gb"] for r in self._rows}
        today = date.today()
        out: list[dict] = []
        for i in range(days - 1, -1, -1):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            out.append({"day": d, "used_gb": mapping.get(d, 0.0)})
        return out

    def month_total_gb(self) -> float:
        """本月（daily 表中同月）累计用量。"""
        month = datetime.now().strftime("%Y-%m")
        return sum(r["used_gb"] for r in self._rows if r["month"] == month)
