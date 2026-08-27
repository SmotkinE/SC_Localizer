"""
Обновление самой программы с GitHub Releases.

Перевод программа обновляет сама и давно (github_source.py). Здесь — про то,
как обновить себя, чтобы не рассылать друзьям новый архив после каждой правки.

Как это работает:
  1. При запуске смотрим последний релиз в UPDATE_REPO и сравниваем теги.
  2. Новее — говорим об этом в интерфейсе, но ничего не трогаем без кнопки.
  3. Нажали — качаем zip, распаковываем во временную папку, проверяем, что там
     действительно программа, и запускаем bat-файл.
  4. bat ждёт, пока exe закроется (свои файлы Windows держит занятыми),
     копирует новые поверх старых и запускает программу обратно.

Настройки и кэш перевода при этом не трогаются: robocopy без /MIR лишнего
не удаляет, а paths.json, profile.json, cache/ и output/ в архив не попадают.
"""
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from config import Config
from github_source import API, GitHubError, download_file, http_get
from logger import get_logger
from version import APP_VERSION

log = get_logger(__name__)

# Репозиторий с релизами программы. Меняется через .env, если переедет.
UPDATE_REPO = os.getenv('UPDATE_REPO', 'SmotkinE/SC_Localizer')

EXE_NAME = 'SC_Localizer.exe'
UPDATE_DIR_NAME = 'sc_localizer_update'

# Имя bat-файла, который доделывает работу после выхода программы.
APPLY_BAT = 'apply_update.bat'


class UpdateError(Exception):
    """Обновление не удалось."""


@dataclass
class AppRelease:
    version: str    # 1.2.0
    tag: str        # v1.2.0
    notes: str      # описание релиза
    url: str        # прямая ссылка на zip
    size: int       # размер zip, для проверки скачанного
    date: str


def _as_numbers(tag: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3). Из чего не выцарапать чисел, считаем нулевым."""
    nums = re.findall(r'\d+', tag or '')
    return tuple(int(n) for n in nums[:4]) if nums else (0,)


def is_newer(tag: str, current: str = APP_VERSION) -> bool:
    """Сравниваем по числам, а не по строкам: '1.10' строкой меньше '1.9'."""
    return _as_numbers(tag) > _as_numbers(current)


def updates_supported() -> bool:
    """
    Обновлять себя умеет только собранная программа.

    Из исходников подменять файлы бессмысленно и опасно: там лежит рабочая
    копия, а не раздаваемая сборка.
    """
    return bool(Config.IS_FROZEN and UPDATE_REPO)


def latest_release() -> AppRelease | None:
    """
    Последний релиз программы. None — если релизов ещё нет или в них нет архива.

    Пустой репозиторий — обычное дело в самом начале, и падать из-за этого
    программа не должна: обновление не главная её работа.
    """
    if not UPDATE_REPO:
        return None
    try:
        data = http_get(f'{API}/repos/{UPDATE_REPO}/releases/latest').json()
    except GitHubError as e:
        if 'Не найдено' in str(e):
            log.info('В %s пока нет релизов программы', UPDATE_REPO)
            return None
        raise

    tag = data.get('tag_name', '')
    asset = next((a for a in data.get('assets', [])
                  if a.get('name', '').lower().endswith('.zip')), None)
    if not asset:
        log.warning('В релизе %s нет zip-архива, обновляться нечем', tag)
        return None

    return AppRelease(
        version=tag.lstrip('vV'),
        tag=tag,
        notes=(data.get('body') or '').strip(),
        url=asset.get('browser_download_url', ''),
        size=int(asset.get('size') or 0),
        date=(data.get('published_at') or '')[:10],
    )


def _app_root_in(folder: Path) -> Path:
    """
    Где внутри распакованного архива лежит программа.

    Архив собирают по-разному: exe может быть и в корне, и в папке
    SC_Localizer/. Ищем по самому exe, а не по имени папки.
    """
    if (folder / EXE_NAME).is_file():
        return folder
    for found in folder.rglob(EXE_NAME):
        return found.parent
    raise UpdateError(f'В архиве обновления нет {EXE_NAME} — '
                      'похоже, выложен не тот файл')


def stage_update(release: AppRelease) -> Path:
    """
    Качает и распаковывает новую версию во временную папку.

    Ничего рабочего не трогает: до запуска bat-файла программу можно закрыть
    в любой момент без последствий.
    """
    if not release.url:
        raise UpdateError('У релиза нет ссылки на архив')

    tmp = Path(tempfile.gettempdir()) / UPDATE_DIR_NAME
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    archive = tmp / 'update.zip'
    log.info('Качаю обновление %s', release.tag)
    download_file(release.url, archive,
                  expected_size=release.size or None, expected_md5=None)

    unpacked = tmp / 'new'
    try:
        with zipfile.ZipFile(archive) as z:
            z.extractall(unpacked)
    except (zipfile.BadZipFile, OSError) as e:
        raise UpdateError(f'Архив обновления не читается: {e}') from e
    archive.unlink(missing_ok=True)

    root = _app_root_in(unpacked)
    # Без _internal собранная программа не запустится, а мы к тому моменту уже
    # перезапишем рабочую. Лучше отказаться сейчас, пока ничего не тронуто.
    if not (root / '_internal').is_dir():
        raise UpdateError('В архиве обновления нет папки _internal — '
                          'такая сборка не запустится')
    log.info('Обновление распаковано в %s', root)
    return root


# Русский текст в bat-файле пришлось бы писать в cp866, поэтому сообщения об
# ошибке здесь латиницей: файл видит только тот, у кого обновление не доехало,
# а кракозябры в такой момент помогают меньше всего.
#
# /E — со всеми подпапками, /IS /IT — перезаписывать даже совпадающие файлы.
# Без /MIR robocopy ничего лишнего не удаляет, поэтому настройки, cache/
# и output/ у человека остаются на месте.
_BAT_TEMPLATE = '''@echo off
cd /d "%~dp0"

set TRIES=0
:wait
tasklist /FI "IMAGENAME eq {exe}" 2>nul | find /I "{exe}" >nul
if errorlevel 1 goto copyfiles
set /a TRIES+=1
if %TRIES% GEQ 60 goto giveup
timeout /t 1 /nobreak >nul
goto wait

:copyfiles
robocopy "{src}" "{dst}" /E /IS /IT /R:3 /W:2 /NFL /NDL /NJH /NJS >nul
if errorlevel 8 goto giveup

start "" /D "{dst}" "{dst}{sep}{exe}"
cd /d "%TEMP%"
rmdir /S /Q "{tmp}" >nul 2>&1
exit /b 0

:giveup
echo.
echo  SC Localizer: update failed.
echo  New version is here:
echo    {src}
echo  Copy files from there into:
echo    {dst}
echo.
pause
'''


def apply_update(staged_root: Path) -> None:
    """
    Запускает bat, который подменит файлы после выхода программы, и возвращается.

    Сама программа должна закрыться сразу после этого вызова: пока exe жив,
    Windows не даст перезаписать ни его, ни библиотеки рядом.
    """
    if not Config.IS_FROZEN:
        raise UpdateError('Обновлять можно только собранную программу')

    target = Config.BASE_DIR
    # staged_root — это <tmp>/new/... , а убрать надо весь <tmp>.
    tmp = Path(tempfile.gettempdir()) / UPDATE_DIR_NAME
    bat = tmp / APPLY_BAT

    bat.write_text(
        _BAT_TEMPLATE.format(exe=EXE_NAME, src=str(staged_root), dst=str(target),
                             tmp=str(tmp), sep=os.sep),
        encoding='cp866', errors='replace')

    # DETACHED_PROCESS обязателен: иначе bat умрёт вместе с программой,
    # выхода которой он как раз и ждёт, и обновление не доедет.
    subprocess.Popen(
        ['cmd', '/c', str(bat)],
        cwd=str(tmp),
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    log.info('Запущен %s, жду выхода программы', bat)
