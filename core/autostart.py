"""开机自启动（只写 HKCU\\...\\Run，不需要管理员权限）。

为什么不用启动文件夹的快捷方式：那需要一个 .lnk，生成 .lnk 要么拉 pywin32，
要么手写 COM，代价远大于往注册表写一个字符串。

注册表是**唯一事实来源** —— 设置对话框每次都现场读一遍，而不是把开关状态
另存在 config.json 里。否则用户手动删掉了注册表项，界面还会显示"已开启"。
"""
from __future__ import annotations

import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "NetPing"


def _command() -> str:
    """写进注册表的启动命令行。

    打包成 exe 就直接指 exe；开发态用 pythonw 跑 app.py
    （pythonw 不弹黑框，否则每次开机都会闪一个控制台窗口）。
    """
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}"'
    app = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"
    )
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return f'"{pyw}" "{app}"'


def is_enabled() -> bool:
    """注册表里有没有我们的自启动项。读不到（键不存在/无权限）都算没开。"""
    try:
        import winreg
    except ImportError:      # 非 Windows，直接当没开
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
        return bool(value)
    except OSError:
        return False


def set_enabled(on: bool) -> tuple[bool, str]:
    """开关自启。返回 (是否成功, 失败原因)。

    失败不抛异常 —— 设置对话框得把原因显示给用户（组策略锁了 Run 键之类）。
    """
    try:
        import winreg
    except ImportError:
        return False, "当前系统不支持（不是 Windows）"
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if on:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass          # 本来就没有，等同于"已经关掉"
        return True, ""
    except OSError as exc:
        return False, str(exc)


def describe() -> str:
    """给界面显示用的说明文本。"""
    return f"注册表项：HKCU\\{RUN_KEY}\\{VALUE_NAME}"
