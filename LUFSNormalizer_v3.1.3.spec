# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['normalize_gui_modern.py'],
    pathex=[],
    binaries=[],
    datas=[('config.default.json', '.'), ('lufs_normalizer', 'lufs_normalizer')],
    hiddenimports=['PySide6.QtWidgets', 'PySide6.QtCore', 'PySide6.QtGui', 'soundfile', 'pyloudnorm', 'soxr', 'numpy', 'watchdog'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtWebEngine', 'PySide6.Qt3D', 'PySide6.QtMultimedia', 'PySide6.QtQuick', 'customtkinter', 'tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LUFSNormalizer_v3.1.3',
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
    icon=['icons\\app_icon.ico'],
)
