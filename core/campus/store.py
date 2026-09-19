"""SQLite 持久化：额度快照、日汇总、设备事件与别名。

所有方法都带锁，可被工作线程安全调用。

计量单位说明：快照表存 MB（`used_mb`，主指标口径），日汇总也存 MB
（`start_mb` / `end_mb`）。第一代存的是 KB，本版统一成 MB —— 主指标本来就是
接口返回的 MB，来回换算只会引入浮点误差。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from core.config import data_dir

from .models import MB_PER_GB, Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    account     TEXT    NOT NULL,
    online      INTEGER NOT NULL,
    source      TEXT    NOT NULL DEFAULT '',
    used_mb     REAL    NOT NULL DEFAULT 0,
    flow_kb     INTEGER NOT NULL DEFAULT 0,
    v6_mb       REAL    NOT NULL DEFAULT 0,
    balance_raw INTEGER NOT NULL DEFAULT 0,
    minutes     INTEGER NOT NULL DEFAULT 0,
    v4ip        TEXT,
    v6ip        TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshot(ts);

CREATE TABLE IF NOT EXISTS daily (
    day         TEXT PRIMARY KEY,
    month       TEXT NOT NULL,
    start_mb    REAL NOT NULL,
    end_mb      REAL NOT NULL,
    peak_rate   REAL NOT NULL DEFAULT 0,
    samples     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_daily_month ON daily(month);

CREATE TABLE IF NOT EXISTS device_event (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    mac      TEXT NOT NULL,
    ip       TEXT,
    host     TEXT,
    event    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_alias (
    mac     TEXT PRIMARY KEY,
    alias   TEXT,
    trusted INTEGER NOT NULL DEFAULT 0,
    note    TEXT
);
"""


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _month(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m")


class Store:
    """额度历史与设备的本地存储。

    关闭后所有读写都变成安全空操作（见 _closed）——退出流程里关库之后，
    界面的定时器还可能被事件循环推一次，那时撞上裸的 sqlite3.ProgrammingError
    就会在退出画面上弹一个没人看得懂的报错框。
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else Path(data_dir()) / "netone.db"
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------ 快照
    def save_snapshot(self, snap: Snapshot) -> None:
        if self._closed:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO snapshot (ts, account, online, source, used_mb, flow_kb,"
                " v6_mb, balance_raw, minutes, v4ip, v6ip) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (snap.ts, snap.account, int(snap.online), snap.source, snap.used_mb,
                 snap.flow_kb, snap.v6_mb, snap.balance_raw, snap.minutes,
                 snap.v4ip, snap.v6ip),
            )
            self._conn.commit()

    def latest(self) -> Snapshot | None:
        if self._closed:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM snapshot ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return self._row_to_snap(row) if row else None

    def latest_before(self, ts: float) -> Snapshot | None:
        if self._closed:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM snapshot WHERE ts < ? ORDER BY ts DESC LIMIT 1", (ts,)
            ).fetchone()
        return self._row_to_snap(row) if row else None

    @staticmethod
    def _row_to_snap(row: sqlite3.Row) -> Snapshot:
        return Snapshot(
            ts=row["ts"],
            account=row["account"],
            online=bool(row["online"]),
            source=row["source"] or "",
            used_mb=row["used_mb"],
            flow_kb=row["flow_kb"],
            v6_mb=row["v6_mb"],
            balance_raw=row["balance_raw"],
            minutes=row["minutes"],
            v4ip=row["v4ip"] or "",
            v6ip=row["v6ip"] or "",
        )

    # ------------------------------------------------------------ 日汇总
    def upsert_daily(self, snap: Snapshot) -> None:
        """按天聚合。用「上一天末值」作为当天起点，避免程序关机时段被漏算。"""
        if not snap.online:
            return
        day, month = _day(snap.ts), _month(snap.ts)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM daily WHERE day=?", (day,)
            ).fetchone()
            if row is None:
                prev = self._conn.execute(
                    "SELECT end_mb FROM daily WHERE day<? ORDER BY day DESC LIMIT 1",
                    (day,),
                ).fetchone()
                start = prev["end_mb"] if prev else snap.used_mb
                self._conn.execute(
                    "INSERT INTO daily (day, month, start_mb, end_mb, samples)"
                    " VALUES (?,?,?,?,1)",
                    (day, month, start, snap.used_mb),
                )
            else:
                prev_snap = self._conn.execute(
                    "SELECT ts, used_mb FROM snapshot WHERE ts < ?"
                    " AND online=1 ORDER BY ts DESC LIMIT 1", (snap.ts,)
                ).fetchone()
                rate = 0.0
                if prev_snap:
                    dt = snap.ts - prev_snap["ts"]
                    if dt > 0:
                        rate = max(0.0, snap.used_mb - prev_snap["used_mb"]) / dt
                self._conn.execute(
                    "UPDATE daily SET end_mb=?, samples=samples+1,"
                    " peak_rate=MAX(peak_rate,?) WHERE day=?",
                    (snap.used_mb, rate, day),
                )
            self._conn.commit()

    def daily_rows(self, limit: int = 40) -> list[dict]:
        """按天返回用量，按时间升序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, month, start_mb, end_mb, samples FROM daily"
                " ORDER BY day DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in reversed(rows):
            used = max(0.0, r["end_mb"] - r["start_mb"])
            out.append({
                "day": r["day"],
                "month": r["month"],
                "used_mb": used,
                "used_gb": used / MB_PER_GB,
                "samples": r["samples"],
            })
        return out

    # ------------------------------------------------------------ 月初清零
    def check_month_reset(self, snap: Snapshot) -> bool:
        """判断服务端是否已切换到新的计费周期。

        两条判据：
        1. 已用流量骤降到前值一半以下（服务端清零）
        2. 本地日期已跨月（兜底）
        """
        prev = self.latest_before(snap.ts)
        if prev is None:
            return False
        if prev.used_mb > 0 and snap.used_mb < prev.used_mb * 0.5:
            return True
        return _month(prev.ts) != _month(snap.ts)

    def reset_today(self, snap: Snapshot) -> None:
        """清零后重建当天记录，避免用量出现巨大负值。"""
        day = _day(snap.ts)
        with self._lock:
            self._conn.execute("DELETE FROM daily WHERE day=?", (day,))
            self._conn.execute(
                "INSERT INTO daily (day, month, start_mb, end_mb, samples)"
                " VALUES (?,?,?,?,1)",
                (day, _month(snap.ts), snap.used_mb, snap.used_mb),
            )
            self._conn.commit()

    # ------------------------------------------------------------ 维护
    def clear_usage(self) -> int:
        """清空额度历史（快照 + 日汇总）。设备别名与事件不动。

        返回删掉的原始快照条数，供界面播报"删了多少"。
        """
        if self._closed:
            return 0
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]
            self._conn.execute("DELETE FROM snapshot")
            self._conn.execute("DELETE FROM daily")
            self._conn.commit()
        return int(n)

    def prune(self, keep_months: int = 12) -> None:
        """清理过期原始快照，保留日汇总。"""
        if self._closed:
            return
        cutoff = time.time() - keep_months * 31 * 86400
        with self._lock:
            self._conn.execute("DELETE FROM snapshot WHERE ts < ?", (cutoff,))
            self._conn.execute("DELETE FROM device_event WHERE ts < ?", (cutoff,))
            self._conn.commit()
            self._conn.execute("VACUUM")

    def close(self) -> None:
        """关闭数据库。可重复调用（退出流程可能走两遍）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    # ------------------------------------------------------------ 设备
    def save_device_event(self, mac: str, event: str, ip: str = "", host: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO device_event (ts, mac, ip, host, event) VALUES (?,?,?,?,?)",
                (time.time(), mac, ip, host, event),
            )
            self._conn.commit()

    def set_alias(self, mac: str, alias: str = "", trusted: bool = False,
                  note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO device_alias (mac, alias, trusted, note) VALUES (?,?,?,?)"
                " ON CONFLICT(mac) DO UPDATE SET alias=excluded.alias,"
                " trusted=excluded.trusted, note=excluded.note",
                (mac, alias, int(trusted), note),
            )
            self._conn.commit()

    def aliases(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM device_alias").fetchall()
        return {r["mac"]: {"alias": r["alias"], "trusted": bool(r["trusted"]),
                           "note": r["note"]} for r in rows}
