# PyInstaller spec: builds a single ResinPilot.exe that starts the server and opens the browser.
#   pyinstaller packaging/resinpilot.spec --noconfirm
# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
PKG = ROOT / "resinpilot"

datas = [
    (str(PKG / "data"), "resinpilot/data"),
    (str(PKG / "assets"), "resinpilot/assets"),
    (str(PKG / "web"), "resinpilot/web"),
]
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("resinpilot")
    + ["sklearn.linear_model", "sklearn.pipeline", "sklearn.preprocessing"]
)

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="ResinPilot",
    icon=str(PKG / "assets" / "branding" / "favicon.ico"),
    console=True,
    upx=False,
)
