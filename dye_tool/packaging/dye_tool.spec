# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格（one-folder，设计文档 §7 兼容性 / M4）。

用法（项目根）：
    .venv\\Scripts\\pyinstaller.exe packaging\\dye_tool.spec --noconfirm --clean

产物：dist/dye_tool/dye_tool.exe（含 config.yaml 与 README，免安装、双击即用）。
"""
from pathlib import Path

# spec 文件的执行期 cwd = 调用 pyinstaller 的目录（约定为项目根）
ROOT = Path(SPECPATH).resolve().parent          # noqa: F821 —— PyInstaller 注入

datas = [
    (str(ROOT / "config.yaml"), "."),            # 默认用户配置
]
# 使用说明/样例数据不进入公开仓库；本地存在时可选打包。
if (ROOT / "README.md").is_file():
    datas.append((str(ROOT / "README.md"), "."))
if (ROOT / "samples").is_dir():
    datas.append((str(ROOT / "samples"), "samples"))
# 中文字体相关的可选资源（若存在一并打包，缺失不影响运行）
for extra in ("README_设计说明.md",):
    p = ROOT / extra
    if p.exists():
        datas.append((str(p), "."))

# ══════════════ hiddenimports：自动扫描（V2.0 α 批 / FR-18 / A20） ══════════════
# 原来这里是**手工枚举** 28 条：每加一个模块都得记得同步，漏了**只在运行时炸**
# （C4 / 幽灵 #8；`--gui-smoke` 能抓，但要先重建整个 exe）。现在改成扫描文件系统
# ——唯一真相就是磁盘上的 .py，新增模块零维护。
#
# 实测（lane-spec，2026-10-16）：手工枚举当时已漏 13 个模块
# （core.dyes/excel_export/explain/extinction/io_fds/parallel_render/plotstyle/
# qc_dashboard/reorg_runs/wavelength_table + ui.widgets 的 dye_library_page/
# dye_time_panel/plot_style_dialog）。它们**今天**都还能被 PyInstaller 的字节码
# import 扫描兜住（静态 import 点都在已列模块里），所以漂移是维护性隐患而非当场故障
# ——A20 仍按"集合相等"钉死，不依赖"现在没炸"。
#
# spec 的执行环境（实测 PyInstaller 6.22.3，`building/build_main.py:1182-1213`）：
# 注入 17 个名字（SPECPATH/SPEC/specnm/DISTPATH/workpath + 5 个构建类 + os），
# **没有 `__file__`**（用了就 NameError），**不改 cwd**。所以只能靠 `SPECPATH`。
# `SPECPATH` 本身是 **str**（不是 Path）——`str / str` 会 TypeError（幽灵 #5）。
def _project_modules(*dirs: str) -> list:
    """扫描项目内的 .py，返回可 import 的模块名（排序，含包名）。

    * ``core/io.py`` → ``core.io``；``ui/pages/phi/page.py`` → ``ui.pages.phi``
    * ``core/__init__.py`` → ``core``；``core/io/__init__.py`` → ``core.io``
      （δ 批 FS1 子包化后仍正确：包名取父目录的点分路径）
    * 跳过 ``__pycache__``（.pyc 不该进 hiddenimports）
    """
    out = []
    for d in dirs:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            rel = p.relative_to(ROOT).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
                if not parts:
                    continue
            out.append(".".join(parts))
    return sorted(set(out))


hiddenimports = _project_modules("core", "ui") + [
    # ⚠️ matplotlib 后端必须显式列出（延迟 import + savefig 按扩展名动态挑后端）。
    # V1.2 起不再输出 PDF，backend_pdf 已移除，只保留 svg 与 agg。
    "matplotlib.backends.backend_svg",
    "matplotlib.backends.backend_agg",
    # ⚠️ 第三方依赖里的**延迟 import** 也要显式列（V2.0 β）：``olefile`` 是 .spc
    # 解析的依赖，在 core/io_spc.py 里是延迟 import（缺依赖时给中文提示而不是
    # 崩在 import 期），PyInstaller 的字节码扫描未必收得到 → 漏了就是"exe 里
    # 一读 .spc 才炸"，而门①（纯数值自检）根本测不出来。
    "olefile",
]
# 显式排除用不到的重型 Qt 模块与库，显著减小 one-folder 体积
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick", "PySide6.QtQml", "PySide6.Qt3DCore", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSpatialAudio", "PySide6.QtStateMachine",
    "PySide6.QtSvgWidgets", "PySide6.QtUiTools", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    # V1.3 P3：下面这些是**实测**发现"排了仍被打进来"的传递依赖（见体积审计），
    # 补进 excludes 才能真的不拷。每一项都由 --gui-smoke（门②）验证过。
    "PySide6.QtVirtualKeyboard",   # 平台输入法插件 → Qt6VirtualKeyboard.dll + 插件
    "PySide6.QtPdf",               # Qt 的 PDF 引擎（本项目不出 PDF）→ Qt6Pdf.dll 4.4 MB
    "PySide6.QtQmlModels", "PySide6.QtQmlMeta", "PySide6.QtQmlWorkerScript",
    "PySide6.QtNetwork",           # 桌面单机工具不联网 → Qt6Network + libcrypto/ssl ~10 MB
    # 注意：不要排除 scipy.optimize._highspy —— scipy>=1.11 中
    # scipy.optimize._linprog_highs 在 import 期就依赖它，而 scipy.stats
    # 会 import scipy.optimize，排除后打包产物启动即 ModuleNotFoundError。
    # 只导出 png/svg，不需要 AVIF 编解码插件（~7 MB）
    "PIL.AvifImagePlugin", "PIL._avif",
    "tkinter", "pandas", "IPython", "pytest", "PyInstaller",
]

# ── P3 体积审计的实测结论（每一项都由 tools/bundle_audit.py 量化、门② 验证）──
# ① PySide6/translations：99 个 .qm 文件 6.70 MB。本应用界面全中文且**不用**
#    Qt 自带翻译（自己的字符串直接写死），实测删除后门② 全过。
# ② matplotlib/mpl-data/sample_data：0.50 MB，只给 mpl 官方示例用。
# ③ 只导出 png/svg，Qt 的其它图片格式插件用不上（tiff/webp/jpeg/icns/tga/...）。
#    注意 share/icons 里的 svg 图标要靠 qsvg 插件 + Qt6Svg.dll，**不能删**。
# ④ libcrypto-1_1.dll / libssl-1_1.dll（3.96 MB）是 OpenSSL 1.1 旧版；
#    PySide6 6.12 的 TLS 后端用 3.x（libcrypto-3-x64.dll）。随 QtNetwork 一起消失。
_DROP_DATA_DIRS = (
    "pyside6/translations",
    "matplotlib/mpl-data/sample_data",
)
_DROP_PLUGINS = (
    "pyside6/plugins/imageformats/qtiff.dll",
    "pyside6/plugins/imageformats/qwebp.dll",
    "pyside6/plugins/imageformats/qjpeg.dll",
    "pyside6/plugins/imageformats/qicns.dll",
    "pyside6/plugins/imageformats/qtga.dll",
    "pyside6/plugins/imageformats/qwbmp.dll",
    "pyside6/plugins/generic/qtuiotouchplugin.dll",
    "pyside6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
    "pyside6/plugins/imageformats/qpdf.dll",
)
# ⑤ QML 全家 + Qt 的 PDF 引擎 + 网络栈。**excludes 对它们无效**（实测：排了仍在），
#    因为它们是**别的 DLL 的硬依赖**，PyInstaller 按 PE 依赖表照拷：
#        Qt6VirtualKeyboard <- Qt6Quick           （平台输入法插件拉进来的）
#        Qt6Quick           <- Qt6Qml, Qt6QmlModels, Qt6Network
#        Qt6Pdf             <- qpdf.dll(图片格式插件)
#        Qt6Network         <- Qt6Pdf, Qt6Qml, Qt6Quick
#    所以要按**文件**删，而且要从"入度为 0 的那个"开始删（删 Qt6Pdf 与 Qt6Quick
#    之后 Network 才没人要）。本应用只用 QtWidgets + QtGui（自绘光栅画布），
#    不联网、不出 PDF、不用 QML —— 每一项都由 ``--gui-smoke``（门②）验证过。
_DROP_QT_MODULES = (
    "qt6virtualkeyboard.dll",
    "qt6quick.dll", "qt6qml.dll", "qt6qmlmodels.dll", "qt6qmlmeta.dll",
    "qt6qmlworkerscript.dll",
    "qt6pdf.dll",
    "qt6network.dll", "qtnetwork.pyd",
)
_DROP_LIBS = (
    # OpenSSL 1.1 旧版（_hashlib/_ssl 用）
    "libcrypto-1_1.dll", "libssl-1_1.dll",
    # OpenSSL 3.x：只被 Qt6Network 需要；上面删了 Network 后它就成了孤儿
    "libcrypto-3-x64.dll", "libssl-3-x64.dll",
)


def _norm(path: str) -> str:
    """把 PyInstaller 的目标路径统一成小写正斜杠，便于前缀比较。"""
    return str(path).replace("\\", "/").lower()


a = Analysis(                                     # noqa: F821
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# ── 精确剔除体积大且本应用确定不需要的二进制 ──
# opengl32sw.dll 是 Qt 的软件 OpenGL 回退（~20 MB）。本应用仅用 QtWidgets +
# matplotlib 的 QPainter 光栅画布，不创建 OpenGL 上下文，故可安全移除。
# 若目标机器上确实需要，从 PySide6 包目录拷回 _internal/PySide6/ 即可。
_DROP_BINARIES = ("opengl32sw.dll",)
a.binaries = [b for b in a.binaries
              if not any(b[0].lower().endswith(d) for d in _DROP_BINARIES)]

# ② 数据目录与插件：按**目标相对路径**过滤（datas 的 dest 才是真实落点）
a.datas = [d for d in a.datas
           if not any(_norm(d[0]).startswith(p) or _norm(d[1]).startswith(p)
                      for p in _DROP_DATA_DIRS)]
# 插件是 binaries 形态（dest 形如 PySide6/plugins/imageformats/qtiff.dll）
a.binaries = [b for b in a.binaries
              if not any(_norm(b[0]) == p or _norm(b[1]) == p
                         for p in _DROP_PLUGINS)
              and not any(_norm(b[0]).endswith("/" + p) or _norm(b[0]) == p
                          for p in _DROP_LIBS)
              and not any(_norm(b[0]).lower().endswith("/" + p)
                          or _norm(b[0]).lower() == p
                          for p in _DROP_QT_MODULES)]

pyz = PYZ(a.pure)                                 # noqa: F821

exe = EXE(                                        # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="dye_tool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                                # GUI 程序，不弹控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    # L17（评审记录）：给 exe 加 Windows 版本资源，右键"属性 → 详细信息"能看到
    # 产品名与版本号。文件里的版本字段由 packaging/build.ps1 按 core.VERSION 自动
    # 改写，所以版本号仍然只有一处真相。
    # ⚠ ROOT 是**项目根**（SPECPATH 的父目录），不是 spec 所在目录 —— 别漏掉 packaging。
    version=str(ROOT / "packaging" / "version_info.txt"),
)
coll = COLLECT(                                   # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="dye_tool",
)
