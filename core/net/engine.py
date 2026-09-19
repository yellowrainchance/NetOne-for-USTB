"""探测调度器。

每个目标一个独立线程，按各自间隔循环探测，结果塞进队列。
UI 层用定时器 drain 队列 —— 刻意避开跨线程 Qt 信号，
队列 + 定时器是最不容易出错的做法。

同时收集"丢包事件"和"延迟尖峰事件"，供事件日志使用。
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

from core.config import Target
from .probe import ProbeResult, probe_once
from .stats import StatsHub

# 样本队列的上限。UI 侧每 250ms 抽一次（单次最多 400 条），
# 5 个目标 1Hz 也才 5 条/秒，正常情况下队列深度常年是个位数。
# 但如果机器休眠 / 系统卡死 / 某个模态循环把事件循环堵住，采集线程仍在照常
# 往里塞，这就是**唯一一个没有上限的容器**，挂一夜能涨到几十万条对象。
QUEUE_MAX = 4000                # 约 13 分钟的积压（5 目标 × 1Hz）


def _trim(q: queue.Queue) -> None:
    """队列超上限时丢掉最旧的。

    丢旧的而不是丢新的：真出现堆积，用户要看的是最近的情况。
    这里的 qsize() 是近似值、并发下可能多丢一两条，但"罕见情况下少一条样本"
    远好过"内存一直涨"。
    """
    while q.qsize() > QUEUE_MAX:
        try:
            q.get_nowait()
        except queue.Empty:
            return


@dataclass
class Sample:
    """从工作线程传回主线程的一条记录。"""

    tid: str
    ts: float
    rtt: float | None
    status: str
    detail: str
    dns_ms: float | None = None
    warmup: bool = False   # 前几次连接常因慢启动/ARP/DNS 而异常，不计入统计


@dataclass
class Event:
    """值得记录的网络事件。"""

    ts: float
    tid: str
    name: str
    kind: str      # loss_start | loss_end | spike | recovered | down | up
    text: str
    severity: str  # info | warn | bad


class _Worker(threading.Thread):
    WARMUP_ROUNDS = 2

    def __init__(self, target: Target, out: queue.Queue):
        super().__init__(daemon=True, name=f"probe-{target.id}")
        self.target = target
        self.out = out
        # 每个线程自带停止信号：这样增删单个目标时只停这一个线程，
        # 不会连累其它目标（用一个共享 Event 就做不到增量）
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _snapshot_target(self) -> Target:
        """每轮探测前拷一份参数。

        target 是配置里那个对象的引用，编辑对话框是**原地改**它的字段的，
        所以这里每轮重新拷一次 —— 改地址/端口/间隔不需要重启线程就能生效。
        """
        return Target(
            id=self.target.id,
            name=self.target.name,
            host=self.target.host,
            port=self.target.port,
            kind=self.target.kind,
            enabled=self.target.enabled,
            interval=self.target.interval,
            timeout_ms=self.target.timeout_ms,
            color=self.target.color,
            role=self.target.role,
            builtin=self.target.builtin,
        )

    def run(self) -> None:
        rounds = 0
        while not self._stop.is_set():
            cfg = self._snapshot_target()
            t0 = time.monotonic()
            try:
                res = probe_once(cfg)
            except Exception as exc:  # 兜底，绝不让线程死掉
                res = ProbeResult(False, status="error", detail=f"探测异常: {exc}")
            rounds += 1
            self.out.put(
                Sample(
                    tid=cfg.id,
                    ts=time.monotonic(),
                    rtt=res.rtt if res.ok else None,
                    status=res.status,
                    detail=res.detail,
                    dns_ms=res.dns_ms,
                    warmup=rounds <= self.WARMUP_ROUNDS,
                )
            )
            _trim(self.out)
            spent = time.monotonic() - t0
            self._stop.wait(max(0.05, cfg.interval - spent))


class Engine:
    """管理所有目标线程，并产出统计与事件。"""

    def __init__(self, hub: StatsHub):
        self.hub = hub
        self.queue: queue.Queue[Sample] = queue.Queue()
        self.events: list[Event] = []
        self._workers: dict[str, _Worker] = {}
        self._running = False
        # 事件状态跟踪
        self._loss_run: dict[str, int] = {}
        self._spike_until: dict[str, float] = {}
        self._last_rtt: dict[str, float] = {}

    # ---------------------------------------------------------- 生命周期

    def start(self, targets: list[Target]) -> None:
        """全量启动：先停掉现有的，再按 targets 重新起一批。"""
        self.stop()
        for target in targets:
            if not target.enabled or not target.host:
                continue
            self._workers[target.id] = self._spawn(target)
        self._running = True

    def sync(self, targets: list[Target]) -> None:
        """增量对齐目标集合：只动真正变化的那几个线程。

        start() 是"先全停再全起" —— 加一个目标会把**所有**目标的探测线程掐掉重来，
        而 stop() 又必须在 UI 线程里逐个 join 等它们退出。所以用户点一下
        "添加目标"，界面就要跟着卡一下，同时其它目标的曲线出现断档。
        增删改目标时走这里：只有被增/被删的那个目标受影响。

        暂停状态下调用是空操作 —— 暂停是你自己的选择，不该被加目标偷偷恢复。
        """
        if not self._running:
            return
        want = {t.id: t for t in targets if t.enabled and t.host}
        for tid in list(self._workers):
            if tid not in want:
                self._drop(tid)
        for tid, target in want.items():
            if tid not in self._workers:
                self._workers[tid] = self._spawn(target)

    def _spawn(self, target: Target) -> _Worker:
        worker = _Worker(target, self.queue)
        worker.start()
        return worker

    def _drop(self, tid: str) -> None:
        """停掉一个线程，但**不等**它。

        它可能正卡在一次超时探测里（最多 1.5 秒）。反正线程是 daemon，
        它会在这一轮结束后自己退出，界面没必要在这儿干等。
        """
        worker = self._workers.pop(tid, None)
        if worker is not None:
            worker.stop()

    def stop(self, timeout: float = 0.6) -> None:
        """停掉所有线程。等待有总时长上限 —— 这是 UI 线程，不能久留。"""
        workers = list(self._workers.values())
        for worker in workers:
            worker.stop()
        deadline = time.monotonic() + timeout
        for worker in workers:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            worker.join(timeout=left)
        self._workers.clear()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    # ---------------------------------------------------------- 消费

    def drain(self, limit: int = 400) -> list[Sample]:
        out: list[Sample] = []
        for _ in range(limit):
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return out

    def ingest(self, samples: list[Sample]) -> list[Event]:
        """把样本写入统计，并生成事件。返回本次新产生的事件。"""
        fresh: list[Event] = []
        for s in samples:
            if s.warmup:
                continue   # 首次连接的异常值不写进统计，否则首屏曲线会被拉爆
            st = self.hub.get(s.tid)
            if st is None:
                continue
            st.add(s.ts, s.rtt, s.status, s.detail)
            ev = self._track(s, st)
            if ev:
                fresh.append(ev)
                self.events.append(ev)
        if len(self.events) > 3000:
            del self.events[: len(self.events) - 3000]
        return fresh

    def clear_history(self) -> None:
        """清空累积的事件，并把事件判定的中间状态一起复位。

        只清 events 不中间状态的话，清空后第一次丢包会被当成"已经在丢包中"
        （_loss_run 还记着上一次的计数），于是丢包事件反而不再产生了。
        """
        self.events.clear()
        self._loss_run.clear()
        self._spike_until.clear()
        self._last_rtt.clear()

    def _track(self, s: Sample, st) -> Event | None:
        name = st.name
        if s.rtt is None:
            run = self._loss_run.get(s.tid, 0) + 1
            self._loss_run[s.tid] = run
            if run == 1:
                return Event(s.ts, s.tid, name, "loss_start",
                             f"{name} 开始丢包（{s.status}）", "warn")
            if run == 5:
                return Event(s.ts, s.tid, name, "down",
                             f"{name} 已连续 5 次无响应，可能完全不通", "bad")
            return None

        # 恢复
        if self._loss_run.get(s.tid, 0) > 0:
            run = self._loss_run[s.tid]
            self._loss_run[s.tid] = 0
            return Event(s.ts, s.tid, name, "loss_end",
                         f"{name} 恢复连通（中断 {run} 次）", "info")

        prev = self._last_rtt.get(s.tid)
        self._last_rtt[s.tid] = s.rtt
        now = s.ts
        if prev is not None and now >= self._spike_until.get(s.tid, 0):
            jump = s.rtt - prev
            if (jump > 60 and s.rtt > prev * 2.0) or s.rtt > 300:
                self._spike_until[s.tid] = now + 3.0
                return Event(s.ts, s.tid, name, "spike",
                             f"{name} 延迟尖峰 {prev:.0f} → {s.rtt:.0f} ms", "warn")
        return None

    # ---------------------------------------------------------- 诊断

    def diagnose_with(self, roles: dict[str, str], window: float = 60.0):
        """roles: {tid: role}。返回诊断结论列表 (severity, title, detail)。"""
        hub = self.hub
        findings: list[tuple[str, str, str]] = []
        by_role: dict[str, list[tuple[str, object]]] = {}
        for tid, role in roles.items():
            st = hub.get(tid)
            if st is None:
                continue
            by_role.setdefault(role, []).append((st.name, st.snapshot(window)))

        def agg(role: str) -> tuple[float, float, float | None]:
            """返回该角色下最差的 (丢包率, 平均延迟, 抖动)。"""
            for _name, snap in by_role.get(role, []):
                if snap.sent >= 5:
                    return snap.loss, (snap.avg or 0.0), snap.jitter
            return 0.0, 0.0, None

        gw_loss, gw_avg, gw_jit = agg("gateway")
        bl_loss, bl_avg, bl_jit = agg("baseline")
        tg_items = by_role.get("target", [])
        tg_worst_loss = 0.0
        tg_worst_name = ""
        for name, snap in tg_items:
            if snap.sent >= 5 and snap.loss > tg_worst_loss:
                tg_worst_loss, tg_worst_name = snap.loss, name

        has_gw = bool(by_role.get("gateway"))
        has_bl = bool(by_role.get("baseline"))
        gw_ready = any(snap.sent >= 8 for _n, snap in by_role.get("gateway", []))
        bl_ready = any(snap.sent >= 8 for _n, snap in by_role.get("baseline", []))
        gw_snap = by_role["gateway"][0][1] if has_gw else None

        if not any(v for v in by_role.values()):
            return [("info", "数据不足", "还没有采到足够样本，先运行十几秒再看诊断结论。")]

        # 1) 本地链路
        if has_gw:
            if gw_snap is not None and gw_snap.sent >= 8 and gw_snap.loss >= 99.0:
                findings.append((
                    "info",
                    "本地网关不响应 ping",
                    "这台路由器/网关屏蔽了 ICMP，所以这一项判断不了本地链路好坏。"
                    "可以删掉这个目标，或者把它的探测方式改成 TCP（端口填路由器管理页的 80）。",
                ))
            elif not gw_ready:
                findings.append(("info", "本地链路：样本不足", "再等十几秒。"))
            elif gw_loss > 1.0 or gw_avg > 20:
                findings.append((
                    "bad",
                    "问题出在本地（电脑 ↔ 路由器）",
                    f"到网关的丢包 {gw_loss:.1f}%、平均 {gw_avg:.0f} ms。"
                    "这一段不走运营商，所以基本可以断定是 WiFi 信号弱 / 路由器拥塞 / 网线或网卡问题。"
                    "优先试：改用网线、靠近路由器、换 5GHz 频段、重启路由器。",
                ))
            else:
                findings.append((
                    "good",
                    "本地链路正常",
                    f"到网关丢包 {gw_loss:.1f}%、平均 {gw_avg:.0f} ms，电脑到路由器这一段没问题。",
                ))

        # 2) 公网出口
        if has_bl:
            if not bl_ready:
                findings.append(("info", "公网线路：样本不足", "再等十几秒。"))
            elif bl_loss > 1.0:
                jit_text = f"{bl_jit:.0f} ms" if bl_jit is not None else "偏高"
                findings.append((
                    "bad",
                    "问题出在运营商出口/公网线路",
                    f"公网基准丢包 {bl_loss:.1f}%、抖动 {jit_text}，"
                    "本地正常而公网抖动丢包，说明是宽带线路或运营商侧拥塞，尤其晚高峰常见。"
                    "可以：换 DNS、报修、或尝试手机热点对比。",
                ))
            elif bl_avg > 100:
                findings.append((
                    "warn",
                    "公网延迟偏高",
                    f"公网基准平均 {bl_avg:.0f} ms，偏高但不丢包，可能是线路绕行或小区出口拥塞。",
                ))
            else:
                findings.append((
                    "good",
                    "公网线路正常",
                    f"公网基准丢包 {bl_loss:.1f}%、平均 {bl_avg:.0f} ms、抖动 {bl_jit or 0:.0f} ms。",
                ))

        # 3) 目标服务器
        if tg_worst_loss > 1.0 and (not has_bl or bl_loss < 1.0):
            findings.append((
                "warn",
                f"只有 {tg_worst_name} 有问题",
                f"{tg_worst_name} 丢包 {tg_worst_loss:.1f}%，而公网基准是好的 —— "
                "说明是对方服务器/进对方机房的线路问题，你能做的有限：换服务器节点或错峰。",
            ))
        elif tg_items and tg_worst_loss <= 1.0:
            findings.append(("good", "目标连接稳定", "关注的目标当前没有丢包，延迟也正常。"))

        return findings
