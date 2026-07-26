# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

binaries = [('ffmpeg.exe', '.')]

for dll_dir in (Path(sys.prefix) / 'Library' / 'bin', Path(sys.prefix) / 'DLLs'):
    for dll_name in ('tcl86t.dll', 'tk86t.dll'):
        dll_path = dll_dir / dll_name
        if dll_path.exists():
            binaries.append((str(dll_path), '.'))

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=binaries,
    datas=[('assets/fonts', 'assets/fonts')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'scipy', 'matplotlib', 'pandas', 'IPython', 'notebook', 'jedi'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MediaEditor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
