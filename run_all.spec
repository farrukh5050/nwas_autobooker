# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

# 🔹 Collect selenium properly (this is the magic sauce)
selenium_datas, selenium_binaries, selenium_hiddenimports = collect_all("selenium")

a = Analysis(
    ['run_all.py'],
    pathex=[],
    binaries=selenium_binaries,
    datas=selenium_datas + [
        ('.env', '.'),  # include your .env next to exe
        ('xl_data', 'xl_data'),  # include your Excel/data folder
        ('json_data', 'json_data'),
    ],
    hiddenimports=selenium_hiddenimports + [
        "selenium.webdriver.chrome.webdriver",
        "selenium.webdriver.chrome.service",
        "selenium.webdriver.chrome.options",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='StreetCars_NWAS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name='StreetCars_NWAS'
)