"""延迟与丢包的滚动统计。

保存原始样本（环形缓冲），所有指标都从样本现算，
这样切换统计窗口（60 秒 / 5 分钟 / 全程）不需要额外数据结构。
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class Snapshot:
    sent: int = 0
    lost: int = 0
    loss: float = 0.0            # 百分比
    last: float | None = None
    mn: float | None = None
    avg: float | None = None
    mx: float | None = None
    jitter: float | None = None  # 相邻成功 RTT 差值的平均（RFC3550 风格）
    p95: float | None = None
    streak_lost: int = 0         # 当前连续丢包次数
    score: float = 0.0           # 0-100 体感评分
    grade: str = "unknown"       # good | fair | poor | bad | unknown


def _grade_of(score: float, sent: int) -> str:
    if sent == 0:
        return "unknown"
    if score >= 80:
        return "good"
    if score >= 60:
        return "fair"
    if score >= 40:
        return "poor"
    return "bad"


class TargetStats:
    """单个目标的样本历史与统计。"""

    MAX_SAMPLES = 7200          # 约 2 小时（1 秒采样）

    def __init__(self, tid: str, name: str, color: str):
        self.id = tid
        self.name = name
        self.color = color
        self.samples: deque[tuple[float, float | None]] = deque(maxlen=self.MAX_SAMPLES)
        self.total_sent = 0
        self.total_lost = 0
        self.streak_lost = 0
        self.last_ip = ""
        self.last_status = ""
        self.last_detail = ""
        self.first_ts: float | None = None

    # ---------------------------------------------------------- 写入

    def add(self, ts: float, rtt: float | None, status: str = "", detail: str = "") -> None:
        self.samples.append((ts, rtt))
        self.total_sent += 1
        self.last_status = status
        self.last_detail = detail
        if rtt is None:
            self.total_lost += 1
            self.streak_lost += 1
        else:
            self.streak_lost = 0
            if detail:
                self.last_ip = detail
        if self.first_ts is None:
            self.first_ts = ts

    def clear(self) -> None:
        self.samples.clear()
        self.total_sent = 0
        self.total_lost = 0
        self.streak_lost = 0
        self.first_ts = None

    # ---------------------------------------------------------- 查询

    def window(self, seconds: float | None) -> list[tuple[float, float | None]]:
        """取出最近 seconds 秒的样本；None 表示全部。"""
        if seconds is None:
            return list(self.samples)
        if not self.samples:
            return []
        cutoff = time.monotonic() - seconds
        # samples 按时间递增，从尾部往前找即可
        out = []
        for item in reversed(self.samples):
            if item[0] < cutoff:
                break
            out.append(item)
        out.reverse()
        return out

    def totals(self) -> Snapshot:
        """只要累计计数，不遍历样本缓冲。

        "累计丢包"每次刷新都要用，但为此扫一遍 7200 条样本纯属浪费——
        计数在 add() 里已经维护好了。
        """
        snap = Snapshot()
        snap.sent = self.total_sent
        snap.lost = self.total_lost
        snap.loss = self.total_lost * 100.0 / self.total_sent if self.total_sent else 0.0
        return snap

    def snapshot(self, seconds: float | None = None) -> Snapshot:
        data = self.window(seconds)
        snap = Snapshot()
        if seconds is None:
            snap.sent = self.total_sent
            snap.lost = self.total_lost
        else:
            snap.sent = len(data)
            snap.lost = sum(1 for _, rtt in data if rtt is None)
        snap.streak_lost = self.streak_lost
        if snap.sent:
            snap.loss = snap.lost * 100.0 / snap.sent

        rtts = [rtt for _, rtt in data if rtt is not None]
        if rtts:
            snap.last = rtts[-1]
            snap.mn = min(rtts)
            snap.mx = max(rtts)
            snap.avg = sum(rtts) / len(rtts)
            ordered = sorted(rtts)
            idx = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
            snap.p95 = ordered[idx]
            if len(rtts) >= 2:
                diffs = [abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))]
                snap.jitter = sum(diffs) / len(diffs)

        snap.score = self._score(snap)
        snap.grade = _grade_of(snap.score, snap.sent)
        return snap

    @staticmethod
    def _score(snap: Snapshot) -> float:
        """把延迟/丢包/抖动折算成 0-100 的"游戏体感分"。"""
        score = 100.0
        score -= min(45.0, snap.loss * 6.0)              # 10% 丢包 -> -45
        if snap.avg is not None:
            score -= max(0.0, snap.avg - 40.0) * 0.25    # 超过 40ms 开始扣，200ms -> -40
        if snap.jitter is not None:
            score -= min(20.0, max(0.0, snap.jitter - 5.0) * 0.6)
        if snap.streak_lost >= 3:
            score -= 10.0
        return max(0.0, min(100.0, score))


class StatsHub:
    """所有目标的统计集合。"""

    def __init__(self):
        self.targets: dict[str, TargetStats] = {}

    def register(self, tid: str, name: str, color: str) -> TargetStats:
        st = TargetStats(tid, name, color)
        self.targets[tid] = st
        return st

    def get(self, tid: str) -> TargetStats | None:
        return self.targets.get(tid)

    def rename(self, tid: str, name: str) -> None:
        """用户改了目标名字后同步过来（事件日志和诊断里显示的都是这个名字）。"""
        st = self.targets.get(tid)
        if st:
            st.name = name
