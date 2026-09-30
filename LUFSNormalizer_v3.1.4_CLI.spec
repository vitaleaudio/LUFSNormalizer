# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['cli_entry.py'],
    pathex=[],
    binaries=[],
    datas=[('config.default.json', '.'), ('lufs_normalizer', 'lufs_normalizer')],
    hiddenimports=['soundfile', 'pyloudnorm', 'soxr', 'numpy', 'watchdog'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['customtkinter', 'tkinter'],
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
    name='LUFSNormalizer_v3.1.4_CLI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icons/app_icon.ico'],
)
