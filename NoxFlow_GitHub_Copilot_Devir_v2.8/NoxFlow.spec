# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "config"), "config"),
    (str(ROOT / "flows"), "flows"),
    (str(ROOT / "certificate"), "certificate"),
]

hiddenimports = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.scrolledtext",
    "PIL",
    "PIL.Image",
    "PIL.ImageGrab",
]

a = Analysis(
    [str(ROOT / "runtime_panel.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "unittest"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NoxFlow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="NoxFlow",
    contents_directory=".",
)
