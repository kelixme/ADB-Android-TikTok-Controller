# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('templates', 'templates'), ('adb.exe', '.'), ('AdbWinApi.dll', '.'), ('libusb-1.0.dll', '.'), ('scrcpy.exe', '.'), ('SDL3.dll', '.'), ('avcodec-63.dll', '.'), ('avformat-63.dll', '.'), ('avutil-61.dll', '.'), ('swresample-7.dll', '.'), ('app.ico', '.'), ('disconnected.png', '.'), ('scrcpy.png', '.'), ('app.bat', '.'), ('scrcpy-server', '.')],
    hiddenimports=['win32file', 'win32event', 'win32api'],
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
    name='ADB TikTok Controller',
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
    icon=['app.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ADB TikTok Controller',
)
import sys 
sys.setrecursionlimit(5000) 
