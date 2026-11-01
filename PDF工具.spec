# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules

datas = []
binaries = []
hiddenimports = ['PySide6.QtPrintSupport']
datas += collect_data_files('pymupdf')
binaries += collect_dynamic_libs('pymupdf')
hiddenimports += collect_submodules('pymupdf')


a = Analysis(
    ['D:/pr/PDF/launch.py'],
    pathex=['D:/pr/PDF'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'pandas', 'scipy', 'tensorflow', 'sklearn', 'numba', 'matplotlib', 'IPython', 'notebook', 'sympy', 'fsspec', 'openpyxl', 'lxml'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PDF工具',
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
    name='PDF工具',
)
