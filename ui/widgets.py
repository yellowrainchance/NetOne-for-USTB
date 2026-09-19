"""公共小控件。

放这里的都是"被两个以上页面用到、而且踩过坑"的东西。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFontMetricsF, QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy

from . import theme


class WrapLabel(QLabel):
    """会自动换行的说明文字标签，**高度永远够用**。

    坑（合并前真实踩过）：`QLabel` 设了 `setWordWrap(True)` 之后，它的高度取决于
    宽度（heightForWidth），但 QFormLayout / QVBoxLayout 分配空间时并不认真对待
    这个约束 —— 空间一紧张就把它压到一行高，文字后半截被直接裁掉。
    原来的设置面板就是这样：三行提示只显示到第二行中间，用户看到的是
    "超时建议不低于" 然后没了。

    这里在每次尺寸变化后把 minimumHeight 同步成 heightForWidth(当前宽度)，
    布局就再也压不动它。宽度变宽时所需行数变少，minimumHeight 会跟着降回去，
    不会留下多余空白。
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)
        pol = self.sizePolicy()
        pol.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        pol.setVerticalPolicy(QSizePolicy.Policy.Minimum)
        self.setSizePolicy(pol)
        self._syncing = False
        # 构造函数里传进来的文字走的是 QLabel 自己的初始化，绕过了下面重写的
        # setText，所以这里补排一次同步。
        # 为什么必须补：只靠 resizeEvent 的话，一个"构造完就有字、但还没被布局
        # 分配过宽度"的标签会一直挂着 minimumHeight=0 —— 此时任何想压扁它的布局
        # 都能压到 0。延到下一轮事件循环，那时宽度通常已经定下来了。
        QTimer.singleShot(0, self._sync_min_height)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_min_height()

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        # 文字换了，所需高度也变了。此刻宽度可能还没定下来，延到下一轮事件循环。
        QTimer.singleShot(0, self._sync_min_height)

    def _sync_min_height(self) -> None:
        if self._syncing:
            return
        width = self.width()
        if width <= 0:
            return
        need = self.heightForWidth(width)
        if need <= 0 or need == self.minimumHeight():
            return
        self._syncing = True
        try:
            self.setMinimumHeight(need)
        finally:
            self._syncing = False


def hint(text: str, parent=None) -> WrapLabel:
    """一行浅色小字说明（设置面板、悬浮窗提示都用它）。"""
    lb = WrapLabel(text, parent)
    lb.setObjectName("Faint")
    return lb


class Badge(QLabel):
    """圆角小徽章。

    刻意不用富文本 HTML 来做：QLabel 对富文本的 sizeHint 是按控件自身字体估的，
    而徽章里的字是 11px，估出来的宽度偏小 —— 窗口一窄，布局就会把它压成一条，
    字全被裁掉（实测 1080 宽的默认窗口下，"整体 良好"只剩一个图标）。
    用样式表写背景和 padding，Qt 会把 padding 算进 sizeHint；再把尺寸策略设成
    Fixed，它就不会被别的控件抢走宽度了。

    另外自带"内容没变就不动"的判断 —— 徽章在刷新循环里每秒都要更新。
    """

    def __init__(self, text: str = "", color: str = "", parent=None):
        super().__init__(text, parent)
        self._state: tuple[str, str] | None = None
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if color:
            self.set_badge(text, color)

    def set_badge(self, text: str, color: str) -> None:
        if self._state == (text, color):
            return
        self._state = (text, color)
        self.setText(text)
        self.setStyleSheet(
            f"background:{color};color:#ffffff;border-radius:7px;"
            f"padding:2px 8px;font-size:11px;font-weight:600;"
        )


_DOT_CACHE: dict[tuple[str, int], QPixmap] = {}


def dot_pixmap(color: str, size: int = 10) -> QPixmap:
    """状态圆点。带缓存——刷新循环里每秒都会用到，别每次都新建 QPixmap 重画一遍。"""
    key = (color, size)
    hit = _DOT_CACHE.get(key)
    if hit is not None:
        return hit
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(color)))
    p.drawEllipse(0, 0, size, size)
    p.end()
    _DOT_CACHE[key] = pm
    return pm


def elide(label: QLabel, text: str, max_w: float) -> None:
    """把文字按宽度省略后放进 label，完整内容挂到 tooltip。

    比 setWordWrap 更适合"只有一行空间、太长就省略"的场合。
    """
    fm = QFontMetricsF(label.font())
    shown = fm.elidedText(text, Qt.TextElideMode.ElideRight, max_w)
    if label.text() != shown:
        label.setText(shown)
    label.setToolTip(text if shown != text else "")


def f1(value, unit: str = "") -> str:
    """None 安全的取整格式化（统计行里到处都是可选值）。"""
    if value is None:
        return "--"
    return f"{value:.0f}{unit}"


__all__ = ["Badge", "WrapLabel", "dot_pixmap", "elide", "f1", "hint", "theme"]
