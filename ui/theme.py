"""界面配色、字体与全局样式表（浅色主题）。

合并前两个项目各有一套浅色配色（NetPing 偏冷蓝、NetQuota 偏暖灰）。
这里统一成 NetPing 那一套 —— 它已经在 1000px 级窗口里验证过对比度，
而且悬浮窗的深色版本也是配套设计的。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 基础色
BG = "#f4f6fa"
CARD = "#ffffff"
BORDER = "#e2e7f0"
BORDER_SOFT = "#eef1f7"
TEXT = "#1e2430"
TEXT_DIM = "#66707f"
TEXT_FAINT = "#98a1b0"
ACCENT = "#2f6fed"
ACCENT_DIM = "#dbe6ff"

# 悬浮窗（在游戏画面上用深色半透明，可读性更好）
OV_BG = "#0f1420"
OV_BORDER = "#2b3548"
OV_TEXT = "#f0f4fb"
OV_DIM = "#93a1b8"
OV_LOSS = "#ff5d47"

# 延迟质量分级
GRADE_COLORS = {
    "good": "#12a150",
    "fair": "#f59e0b",
    "poor": "#f97316",
    "bad": "#e8503a",
    "unknown": "#98a1b0",
}

GRADE_LABEL = {
    "good": "良好",
    "fair": "一般",
    "poor": "较差",
    "bad": "很差",
    "unknown": "采集中",
}

STATUS_LABEL = {
    "ok": "正常",
    "timeout": "超时",
    "unreachable": "不可达",
    "dns_fail": "DNS 失败",
    "error": "错误",
}

# ---------------------------------------------------------------- 额度配色
# 环形进度与进度条按剩余比例上色（红/黄/绿）。
# 注意：这里是"额度"语义，跟涨跌无关，所以低额度用红、充裕用绿。
QUOTA_GREEN = "#12a150"
QUOTA_AMBER = "#f59e0b"
QUOTA_RED = "#e8503a"
QUOTA_GRAY = "#98a1b0"
RING_TRACK = "#e6ebf3"

# 用完额度的警示（超额时的大字与徽章）
QUOTA_BAD = "#c2341c"

FONT_STACK = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'
MONO_STACK = '"Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace'

# 自绘控件用的字体族候选：挑第一个系统里真实存在的，避免中文渲染成方块
_FAMILIES = ["Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "Segoe UI", "SimHei"]
_resolved_family: str | None = None


def resolve_family() -> str:
    global _resolved_family
    if _resolved_family is not None:
        return _resolved_family
    from PySide6.QtGui import QFontDatabase

    try:
        available = set(QFontDatabase.families())
    except Exception:
        available = set()
    for name in _FAMILIES:
        if name in available:
            _resolved_family = name
            break
    else:
        _resolved_family = ""
    return _resolved_family


def ui_font(size: float, bold: bool = False):
    """给自绘控件用的 QFont（确保中文字体可用）。"""
    from PySide6.QtGui import QFont

    f = QFont()
    family = resolve_family()
    if family:
        f.setFamily(family)
    f.setPointSizeF(size)
    f.setBold(bold)
    return f


def quota_color(pct_left: float, online: bool = True) -> str:
    """按剩余百分比选额度颜色（原 NetQuota 的 pick_color，配色并入本主题）。"""
    if not online:
        return QUOTA_GRAY
    if pct_left > 30:
        return QUOTA_GREEN
    if pct_left > 10:
        return QUOTA_AMBER
    return QUOTA_RED


def qss() -> str:
    """全局样式表。"""
    return f"""
    QWidget {{
        font-family: {FONT_STACK};
        color: {TEXT};
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background: {BG}; }}
    QFrame#Card {{
        background: {CARD};
        border: 1px solid {BORDER};
        border-radius: 10px;
    }}
    QLabel#Title {{ font-size: 17px; font-weight: 600; }}
    QLabel#Sub {{ color: {TEXT_DIM}; font-size: 12px; }}
    QLabel#Faint {{ color: {TEXT_FAINT}; font-size: 11px; }}
    QLabel#SectionTitle {{ font-size: 13px; font-weight: 600; color: {TEXT}; }}
    QLabel#Big {{ font-size: 26px; font-weight: 600; }}
    QLabel#MetricLabel {{ color: {TEXT_DIM}; font-size: 12px; }}
    QLabel#MetricValue {{ font-size: 16px; font-weight: 600; }}

    QPushButton {{
        background: {CARD};
        border: 1px solid {BORDER};
        border-radius: 7px;
        padding: 5px 12px;
        color: {TEXT};
    }}
    QPushButton:hover {{ background: #f0f4ff; border-color: #c3d4f7; }}
    QPushButton:pressed {{ background: {ACCENT_DIM}; }}
    QPushButton:disabled {{ color: {TEXT_FAINT}; background: #f7f8fb; }}
    QPushButton#Primary {{
        background: {ACCENT}; border: 1px solid {ACCENT}; color: #ffffff; font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background: #2560d8; }}
    QPushButton#Danger:hover {{ background: #ffeeea; border-color: #f5c1b6; color: #c2341c; }}

    QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
        background: {CARD};
        border: 1px solid {BORDER};
        border-radius: 7px;
        padding: 4px 8px;
        min-height: 20px;
    }}
    QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border-color: {ACCENT};
    }}
    QComboBox::drop-down {{ border: none; width: 20px; }}

    QGroupBox {{
        background: {CARD};
        border: 1px solid {BORDER};
        border-radius: 10px;
        margin-top: 11px;
        padding: 12px 12px 10px 12px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {TEXT};
    }}

    QCheckBox {{ spacing: 7px; color: {TEXT}; }}
    QCheckBox::indicator {{ width: 15px; height: 15px; }}
    QRadioButton {{ spacing: 7px; }}

    /* ---- 标签页：白底 + 选中项底部高亮条 ---- */
    QTabWidget::pane {{
        border: 1px solid {BORDER};
        border-radius: 10px;
        background: {CARD};
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        border: none;
        padding: 7px 18px;
        margin-right: 4px;
        color: {TEXT_DIM};
        font-size: 13px;
    }}
    QTabBar::tab:hover {{ color: {TEXT}; }}
    QTabBar::tab:selected {{
        color: {ACCENT};
        font-weight: 600;
        border-bottom: 2px solid {ACCENT};
    }}

    QScrollArea {{ border: none; background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: #cdd6e6; border-radius: 4px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: #b3c0d6; }}
    QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: #cdd6e6; border-radius: 4px; min-width: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QTextEdit#Events {{
        background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px;
        font-family: {MONO_STACK}; font-size: 12px;
    }}

    QProgressBar {{
        background: {RING_TRACK}; border: none; border-radius: 3px;
    }}
    QProgressBar::chunk {{ border-radius: 3px; }}

    /* ---- 设置窗口左侧导航 ---- */
    QListWidget#Nav {{
        background: transparent;
        border: none;
        outline: none;
        padding: 2px;
    }}
    QListWidget#Nav::item {{
        padding: 7px 10px;
        border-radius: 7px;
        color: {TEXT_DIM};
    }}
    QListWidget#Nav::item:hover {{ background: {BORDER_SOFT}; color: {TEXT}; }}
    QListWidget#Nav::item:selected {{ background: {ACCENT_DIM}; color: {ACCENT}; font-weight: 600; }}

    /* ---- 表格（设备列表）---- */
    QTableWidget {{
        background: {CARD};
        border: 1px solid {BORDER};
        border-radius: 8px;
        gridline-color: {BORDER_SOFT};
        outline: none;
    }}
    QTableWidget::item {{ padding: 4px 6px; }}
    QTableWidget::item:selected {{ background: {ACCENT_DIM}; color: {TEXT}; }}
    QHeaderView::section {{
        background: #f7f9fc;
        border: none;
        border-bottom: 1px solid {BORDER};
        padding: 6px 6px;
        color: {TEXT_DIM};
        font-weight: 600;
    }}

    QToolTip {{
        background: #1e2430; color: #f0f4fb; border: none; padding: 4px 7px;
        border-radius: 4px; font-size: 12px;
    }}
    """
