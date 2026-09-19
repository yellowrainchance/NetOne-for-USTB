# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：NetOne（**散文件夹 / onedir**，不是单文件）。

用法：
    <venv>/python.exe -m PyInstaller --noconfirm --clean NetOne.spec

产物：dist/NetOne/NetOne.exe  + 同目录一堆依赖
数据文件（config.json / netone.db / netone.log）也生成在这个文件夹里，
所以整个 dist/NetOne 拷走就算迁移完成，不写注册表、不碰 %APPDATA%。

## 为什么是 onedir 而不是 onefile

onefile 每次启动都要把几十 MB 依赖解压到 %TEMP% 再加载 —— 首次启动明显慢
（杀软还会对刚解压出来的 exe 重新扫一遍）。onedir 直接加载磁盘上的文件，
启动快得多，代价只是一个文件夹而不是一个 exe。

## 几个刻意的选择

* `upx=False`：UPX 压缩的 DLL 每次加载都要解压，和"启动快"这个目标相反。
* `console=False`：GUI 程序不弹黑框。要看报错请用「排障启动.bat」从源码跑。
* 不引入 QtCharts：两处图表（延迟曲线、日用量柱状图）都是 QPainter 手绘的，
  所以只需要 PySide6-Essentials，不需要体积大几十 MB 的 PySide6-Addons。
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = []
binaries = []
hiddenimports: list[str] = []

# Windows 原生通知（额度告警走它）。装不上时 Notifier 会静默降级，不影响主流程，
# 但打包时要尽量带上，否则装好的程序发不出通知。
try:
    d, b, h = collect_all("windows_toasts")
    datas += d
    binaries += b
    hiddenimports += h
except Exception as exc:      # pragma: no cover - 取决于打包机环境
    print(f"[spec] windows_toasts 收集失败，通知功能将降级：{exc}")

# 显式带上 winrt 子模块：个别环境缺 hooks-contrib 支持时会漏
for mod in ("winrt",
            "winrt.windows.ui.notifications",
            "winrt.windows.data.xml.dom",
            "winrt.windows.foundation",
            "winrt.windows.foundation.collections"):
    try:
        hiddenimports += collect_submodules(mod)
    except Exception:
        pass

# QtNetwork 用于单实例检测（QLocalServer / QLocalSocket）。
# 它通常能被自动分析到，但单实例失败了程序会"静默开两个"，代价太大，
# 所以显式列出来兜住。
hiddenimports += ["PySide6.QtNetwork"]

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 明确排除：这些都不用，带上只会让体积和启动时间变差。
    # QtCharts 特别排除 —— 它属于 PySide6-Addons，本环境根本没装，
    # 万一被误引会直接构建失败，早点暴露比运行时崩好。
    excludes=[
        "tkinter", "matplotlib", "PIL", "pytest", "unittest",
        "PySide6.QtCharts", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore",
        "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtBluetooth",
        "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtOpenGL",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir：依赖交给下面的 COLLECT
    name="NetOne",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # 见文件头：压缩换来的体积不值得拖慢启动
    console=False,                  # GUI 程序，不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["assets/netone.ico"],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NetOne",
)
