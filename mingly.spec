# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Mingly.app (macOS)

block_cipher = None

a = Analysis(
    ['backend/main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('frontend/dist', 'frontend/dist'),
    ],
    hiddenimports=[
        # pywebview / macOS WebKit
        'webview',
        'webview.platforms.cocoa',
        # cryptography (vault encryption)
        'cryptography',
        'cryptography.fernet',
        'cryptography.hazmat.primitives.kdf.pbkdf2',
        'cryptography.hazmat.primitives.hashes',
        'cryptography.hazmat.backends',
        # SQLAlchemy async
        'sqlalchemy.dialects.sqlite',
        'sqlalchemy.dialects.sqlite.aiosqlite',
        'aiosqlite',
        'greenlet',
        # uvicorn internals (not auto-detected)
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.websockets_impl',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        # websockets
        'websockets',
        'websockets.legacy',
        'websockets.legacy.server',
        # httpx (used by Ollama provider)
        'httpx',
        'httpx._transports.default',
        # telethon crypto
        'telethon',
        'telethon.crypto',
        # python-dotenv
        'dotenv',
    ],
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
    [],
    exclude_binaries=True,
    name='Mingly',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # No terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Mingly',
)

app = BUNDLE(
    coll,
    name='Mingly.app',
    icon='assets/icon.icns',
    bundle_identifier='com.mingly.app',
    info_plist={
        'NSHighResolutionCapable': True,
        'CFBundleDisplayName': 'Mingly',
        'CFBundleShortVersionString': '0.1.0',
        'NSAppTransportSecurity': {
            'NSAllowsLocalNetworking': True,
        },
    },
)
