# -*- mode: python ; coding: utf-8 -*-
"""
Сборка автономной портабл-версии.

Собирается в папку (--onedir), а не в один файл: однофайловые сборки
PyInstaller распаковывают себя во временную папку при каждом запуске
и регулярно ловят ложные срабатывания антивирусов на эту самораспаковку.
Для раздачи людям это критично.

Запускать через build.bat, а не напрямую.
"""
from pathlib import Path

SRC = Path(SPECPATH).parent / 'SC_LOCALIZER'

a = Analysis(
    [str(SRC / 'app.py')],
    pathex=[str(SRC)],
    binaries=[],
    # Шаблон страницы обязателен: Flask ищет его на диске, в код он не попадает.
    datas=[(str(SRC / 'templates'), 'templates')],
    # tkinter нужен окну выбора файла, waitress — вместо сервера разработки.
    hiddenimports=['tkinter', 'tkinter.filedialog', 'waitress', 'requests'],
    hookspath=[],
    runtime_hooks=[],
    # Тяжёлые библиотеки, которых в проекте нет и близко.
    excludes=['numpy', 'pandas', 'matplotlib', 'PIL', 'scipy', 'PyQt5', 'PySide2'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SC_Localizer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-сжатие — ещё один повод для антивируса поругаться
    console=False,      # без чёрного окна
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(Path(SPECPATH) / 'sc_localizer.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='SC_Localizer',
)
