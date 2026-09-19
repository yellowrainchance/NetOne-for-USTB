"""网络诊断对话框：告诉你"卡在哪一段"，而不是只丢一堆数字。

结论里最前面几条来自环境检查（直连 / 经代理），因为那会改变下面所有数字的读法。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from core.net.engine import Engine
from core.net.probe import traceroute
from core.config import Target

from . import theme
from .widgets import WrapLabel


class DiagnoseDialog(QDialog):
    def __init__(self, engine: Engine, roles: dict[str, str], targets: list[Target],
                 env=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("网络诊断")
        self.resize(680, 640)
        self.engine = engine
        self.roles = roles
        self.engine_targets = targets
        self.env = env
        self._pending = None
        self.findings: list[tuple[str, str, str]] = []
        self._poll: QTimer | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 14)
        lay.setSpacing(10)

        title = QLabel("问题出在哪一段？")
        title.setObjectName("Title")
        lay.addWidget(title)

        self.lb_conclusion = WrapLabel()
        self.lb_conclusion.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.lb_conclusion)

        row = QHBoxLayout()
        self.cb_host = QComboBox()
        for target in targets:
            if target.role == "gateway":
                continue
            st = engine.hub.get(target.id)
            hint_ip = (st.last_ip if st and st.last_ip else target.host)
            self.cb_host.addItem(f"{target.name}  ({hint_ip})", target.id)
        self.btn_trace = QPushButton("对选中目标执行路由追踪")
        self.btn_trace.clicked.connect(self._run_trace)
        row.addWidget(QLabel("追踪目标"))
        row.addWidget(self.cb_host, 1)
        row.addWidget(self.btn_trace)
        lay.addLayout(row)

        self.txt = QTextEdit()
        self.txt.setReadOnly(True)
        self.txt.setObjectName("Events")
        self.txt.setPlaceholderText("暂无数据")
        lay.addWidget(self.txt, 1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self.refresh()
        self._auto = QTimer(self)
        self._auto.setInterval(2000)
        self._auto.timeout.connect(self.refresh)
        self._auto.start()
        self.setStyleSheet(theme.qss())

    def refresh(self) -> None:
        findings = list(self.engine.diagnose_with(self.roles))
        # 环境结论永远排在最前面：如果是经代理，下面所有数字的读法都要变
        if self.env is not None:
            findings = list(self.env.notes) + findings
        self.findings = findings          # 留一份给测试断言形状是否一致

        html = []
        for severity, head, detail in findings:
            color = {"good": theme.GRADE_COLORS["good"], "warn": theme.GRADE_COLORS["fair"],
                     "bad": theme.GRADE_COLORS["bad"], "info": theme.ACCENT}.get(
                         severity, theme.ACCENT)
            icon = {"good": "✓", "warn": "!", "bad": "×", "info": "i"}.get(severity, "i")
            html.append(
                f'<div style="margin-bottom:10px;">'
                f'<div style="font-weight:600;color:{color};margin-bottom:2px;">'
                f'{icon} {head}</div>'
                f'<div style="color:{theme.TEXT_DIM};font-size:12px;">{detail}</div>'
                f"</div>"
            )
        self.lb_conclusion.setText("".join(html) or "数据不足。")

    def _run_trace(self) -> None:
        tid = self.cb_host.currentData()
        target = next((t for t in self.engine_targets if t.id == tid), None)
        if target is None:
            return
        st = self.engine.hub.get(tid)
        host = (st.last_ip if st and st.last_ip else "") or target.host
        if not host:
            self.txt.setPlainText("该目标还没有解析到地址，等几个探测周期再试。")
            return

        self.btn_trace.setEnabled(False)
        self.txt.setPlainText(f"正在追踪 {host} …（最多 15 跳，大约需要十几秒）")
        name = target.name

        def work() -> None:
            # 子线程只写变量，主线程轮询取走，避免跨线程操作 Qt 对象
            self._pending = (name, host, *traceroute(host))

        threading.Thread(target=work, daemon=True).start()
        if self._poll is None:
            self._poll = QTimer(self)
            self._poll.setInterval(200)
            self._poll.timeout.connect(self._check_trace)
        self._poll.start()

    def _check_trace(self) -> None:
        if self._pending is None:
            return
        name, host, hops, err = self._pending
        self._pending = None
        if self._poll is not None:
            self._poll.stop()
        self._show_trace(name, host, hops, err)

    def _show_trace(self, name: str, host: str, hops, err: str) -> None:
        self.btn_trace.setEnabled(True)
        if err:
            self.txt.setPlainText(f"{name} ({host})\n\n{err}")
            return
        lines = [f"{name}  ({host})", "=" * 46]
        if not hops:
            lines.append("没有拿到任何路由信息（可能被防火墙拦截了 traceroute）。")
        for idx, ip, t in hops:
            tip = ""
            if idx <= 1:
                tip = "    ← 你的路由器"
            elif idx <= 3:
                tip = "    ← 运营商接入"
            elif ip == "*":
                tip = "    ← 这一跳不响应（正常，很多节点会屏蔽）"
            lines.append(f"{idx:>3}   {ip:<16} {t:>10}{tip}")
        self.txt.setPlainText("\n".join(lines))
