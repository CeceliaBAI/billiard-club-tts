# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置文件（支持 macOS 和 Windows）
# 用法: pyinstaller build.spec
import sys

block_cipher = None

# 根据平台选择图标格式
if sys.platform == 'darwin':
    icon_path = 'assets/icon.icns'
elif sys.platform == 'win32':
    icon_path = 'assets/icon.ico'
else:
    icon_path = None

# 跨平台隐藏导入
hiddenimports = [
    'pygame',
    'pygame.mixer',
    'webview',
    'webview.platforms.cocoa',
    'webview.platforms.winforms',
    'pystray',
    'PIL',
    'PIL.Image',
    'PIL.ImageDraw',
    'sounddevice',
    '_sounddevice_data',
]

# 平台特定隐藏导入
if sys.platform == 'darwin':
    hiddenimports += [
        'pystray._darwin',
        'pyobjc',
        'libdispatch',
    ]
elif sys.platform == 'win32':
    hiddenimports += [
        'pystray._win32',
        'win32api',
    ]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config.json', '.'),
        ('audio', 'audio'),
        ('assets', 'assets'),
        ('web', 'web'),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='六六台球播报系统',
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
    icon=icon_path,
)

# macOS: 创建 .app 外壳（解决终端弹窗问题）
if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='六六台球播报系统.app',
        icon=icon_path,
        bundle_identifier='com.billiardclub.tts',
    )
