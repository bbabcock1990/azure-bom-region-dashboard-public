# PyInstaller spec for the Azure BOM Region Dashboard (one-file Windows exe).
#
# Build:  .\.venv\Scripts\pyinstaller.exe bomdash.spec --noconfirm
# Output: dist\AzureBomRegionDashboard.exe  (double-click to launch)
#
# Notes:
#  * server/app.py loads /api/* handlers via importlib.import_module("api.<name>")
#    — a dynamic import PyInstaller cannot see. collect_submodules("api") pulls
#    every handler + _shared module in as a hidden import so the exe has them.
#  * Static assets (app/), demo fixtures, and the JSON/txt/csv catalogs under
#    api/_shared/data are shipped as data files.
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = (
    collect_submodules("api")
    + collect_submodules("server")
    + collect_submodules("uvicorn")
    + collect_submodules("azure.identity")
    + ["anyio", "httpx", "httptools", "websockets"]
)

datas = (
    [("app", "app"), ("fixtures/demo", "fixtures/demo")]
    + collect_data_files("api", includes=["**/*.json", "**/*.txt", "**/*.csv"])
)

a = Analysis(
    ["launch.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "respx"],
    noarchive=False,
)

pyz = PYZ(a.pure)

# One-dir build (COLLECT): the interpreter DLLs sit next to the exe in
# dist\AzureBomRegionDashboard\ rather than being extracted to %TEMP% at launch.
# This is the enterprise-friendly form — it can be code-signed as a folder and
# avoids Application Control policies that block temp-extracted DLLs.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AzureBomRegionDashboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
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
    name="AzureBomRegionDashboard",
)
