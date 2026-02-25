# run_all.spec
block_cipher = None

a = Analysis(
    ['run_all.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('xl_data', 'xl_data'),
        ('.env', '.'),
    ],
    hiddenimports=['get_nwas_data', 'get_address_from_ghost'],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='StreetCars_NWAS',
    debug=False,
    strip=False,
    upx=True,
    console=True,
)