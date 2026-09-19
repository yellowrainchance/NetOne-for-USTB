"""延迟监测历史的落盘与恢复。

`TargetStats.samples` 是内存里的环形缓冲，程序一关就全没了 —— 用户明确要求
「退出时自动保存」，所以这里负责把它写成一个文件，下次启动再读回来。

## 三个必须处理的坑

1. **样本时间戳是 `time.monotonic()`**。单调时钟的起点每个进程都不一样
   （Windows 上通常是开机时刻，但也只保证"单调"，不保证跨进程可比）。
   直接原样存下来再读回来，曲线会横跨一个荒谬的跨度，甚至整段跑到未来去。
   所以落盘时换算成挂钟时间（`time.time()`），读回来再换算回单调时间。

2. **文件必须是有界的**。存的是「最新的 MAX_SAMPLES 条」，和内存里那个环形
   缓冲**同一个上限**。否则跑得越久文件越大，启动时读回来占的内存也越大 ——
   正好和「长时间检测不占过多内存」这个要求相反。

3. **写一半不能留半个文件**。先写 `.tmp` 再 `os.replace()`，这一步在 Windows
   上是原子的（同分区移动）。断电最多丢掉这次保存，不会毁掉上一次的历史。

读取路径上的一切异常都吞掉并返回 0 条：这个文件是"锦上添花"，坏了不该让
程序起不来。格式版本对不上也当空文件处理，不猜。
"""
from __future__ import annotations

import json
import os
import time

from core.config import data_dir

from .stats import TargetStats

HISTORY_FILE = "latency_history.json"
FORMAT_VERSION = 1

# 事件日志只留尾巴。它存的是"哪一秒开始丢包"这类离散事件，
# 翻旧账的价值远不如原始样本，而且单条比样本大得多（有中文文本）。
MAX_EVENTS = 400

# 读文件前的自保上限。正常文件 ~1 MB 量级，超过这个数说明被人手改坏了
# 或者是别的什么东西顶了这个名字，直接当没有。
MAX_FILE_BYTES = 48 * 1024 * 1024

# 样本最大年龄。缓冲最多也就 2 小时（7200 条 @1 秒），留 48 小时是给
# "改了采样间隔"和系统休眠留余量；再老的读回来只会把 X 轴拉得没法看。
MAX_AGE_HOURS = 48.0


def history_path() -> str:
    return os.path.join(data_dir(), HISTORY_FILE)


def _wall(mono_ts: float, now_mono: float, now_wall: float) -> float:
    """单调时间 → 挂钟时间。

    和 `page_latency._export_csv()` 里那套换算是同一个口径：
    先算"这条样本是多久以前"，再从当前挂钟时间往回推。
    """
    return now_wall - (now_mono - mono_ts)


def _mono(wall_ts: float, now_mono: float, now_wall: float) -> float:
    """挂钟时间 → 单调时间（`_wall` 的逆运算）。"""
    return now_mono - (now_wall - wall_ts)


def _blank(path: str) -> dict:
    return {"path": path, "ok": False, "targets": 0, "samples": 0,
            "events": 0, "bytes": 0, "skipped": 0, "saved_at": 0.0}


def save(cfg, hub, engine=None, path: str | None = None) -> dict:
    """把当前所有目标的样本（和事件尾巴）写盘。返回统计摘要。

    不抛异常：这是退出路径上的一步，写不进去也不能拦住退出。
    """
    path = path or history_path()
    now_mono, now_wall = time.monotonic(), time.time()
    out_targets: list[dict] = []
    total = 0

    for target in cfg.targets:
        st = hub.get(target.id)
        if st is None or not st.samples:
            continue
        # 只存最新的 MAX_SAMPLES 条 —— 和内存里的环形缓冲同一个上限
        keep = list(st.samples)[-TargetStats.MAX_SAMPLES:]
        rows = [
            [round(_wall(ts, now_mono, now_wall), 3),
             None if rtt is None else round(float(rtt), 2)]
            for ts, rtt in keep
        ]
        if not rows:
            continue
        out_targets.append({
            "id": target.id,
            "name": target.name,
            "host": target.host,
            "samples": rows,
        })
        total += len(rows)

    events: list[dict] = []
    if engine is not None:
        for ev in list(getattr(engine, "events", []))[-MAX_EVENTS:]:
            events.append({
                "ts": round(_wall(ev.ts, now_mono, now_wall), 3),
                "tid": ev.tid, "name": ev.name, "kind": ev.kind,
                "text": ev.text, "severity": ev.severity,
            })

    data = {
        "version": FORMAT_VERSION,
        "saved_at": round(now_wall, 3),
        "targets": out_targets,
        "events": events,
    }

    summary = _blank(path)
    summary.update({"targets": len(out_targets), "samples": total,
                    "events": len(events), "saved_at": now_wall})
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        summary["bytes"] = os.path.getsize(path)
        summary["ok"] = True
    except OSError as e:
        summary["error"] = str(e)
    return summary


def load(cfg, hub, engine=None, path: str | None = None,
         max_events_in_log: int = 20) -> dict:
    """把上次退出时存的历史读回来，塞进 hub（和 engine.events）。

    目标用 **id** 匹配：配置里已经删掉的目标，它的历史直接丢掉
    （不报错，这是正常情况）。样本按挂钟 → 单调换算后再按时间排序，
    保证 `window()` 那个"从尾部往前扫"的假设依然成立。
    """
    path = path or history_path()
    summary = _blank(path)

    try:
        if not os.path.exists(path):
            return summary
        size = os.path.getsize(path)
        summary["bytes"] = size
        if size > MAX_FILE_BYTES:
            summary["error"] = f"文件过大（{size} 字节），已忽略"
            return summary
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        summary["error"] = str(e)
        return summary

    if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
        summary["error"] = "格式版本不匹配"
        return summary

    now_mono, now_wall = time.monotonic(), time.time()
    cutoff = now_wall - MAX_AGE_HOURS * 3600.0
    known = {t.id for t in cfg.targets}

    for item in data.get("targets") or []:
        if not isinstance(item, dict):
            continue
        st = hub.get(item.get("id"))
        if st is None or item.get("id") not in known:
            continue
        rows = item.get("samples")
        if not isinstance(rows, list):
            continue

        clean: list[tuple[float, float | None]] = []
        for row in rows[-TargetStats.MAX_SAMPLES:]:
            try:
                wall = float(row[0])
                rtt = row[1]
            except (TypeError, ValueError, IndexError):
                summary["skipped"] += 1
                continue
            if wall < cutoff:
                summary["skipped"] += 1
                continue
            clean.append((_mono(wall, now_mono, now_wall),
                          None if rtt is None else float(rtt)))
        if not clean:
            continue

        clean.sort(key=lambda pair: pair[0])
        st.samples.clear()
        st.samples.extend(clean)
        # 累计计数跟着恢复：否则界面写着"累计发 18342 包"，样本缓冲里却只有
        # 7200 条、曲线也只剩 2 小时，两个数字对不上。恢复成"缓冲区内的计数"
        # 之后，"累计"的含义就是「有记录以来」，和能看到的曲线一致。
        st.total_sent = len(clean)
        st.total_lost = sum(1 for _ts, rtt in clean if rtt is None)
        st.streak_lost = 0
        st.first_ts = clean[0][0]
        summary["targets"] += 1
        summary["samples"] += len(clean)

    evs = data.get("events")
    if engine is not None and isinstance(evs, list):
        from .engine import Event

        restored = []
        for raw in evs[-MAX_EVENTS:]:
            if not isinstance(raw, dict):
                continue
            try:
                wall = float(raw["ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if wall < cutoff:
                continue
            restored.append(Event(
                ts=_mono(wall, now_mono, now_wall),
                tid=str(raw.get("tid") or ""),
                name=str(raw.get("name") or ""),
                kind=str(raw.get("kind") or ""),
                text=str(raw.get("text") or ""),
                severity=str(raw.get("severity") or "info"),
            ))
        if restored:
            engine.events.extend(restored)
            summary["events"] = len(restored)
            summary["log_tail"] = restored[-max_events_in_log:]

    summary["ok"] = True
    return summary


def remove(path: str | None = None) -> bool:
    """删掉历史文件及其临时残片。「清空延迟记录」要连文件一起删，
    否则重启后旧数据又回来了 —— 用户会以为清空没生效。
    """
    path = path or history_path()
    gone = False
    for p in (path, path + ".tmp"):
        try:
            if os.path.exists(p):
                os.remove(p)
                gone = gone or p == path
        except OSError:
            pass
    return gone
