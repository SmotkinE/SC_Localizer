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
import base64
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from config import Config
from github_source import GitHubError, download_file, latest_release_tag, release_zip
from logger import get_logger
from version import APP_VERSION

log = get_logger(__name__)

# Репозиторий с релизами программы. Меняется через .env, если переедет.
UPDATE_REPO = os.getenv('UPDATE_REPO', 'SmotkinE/SC_Localizer')

EXE_NAME = 'SC_Localizer.exe'
UPDATE_DIR_NAME = 'sc_localizer_update'

# Имя bat-файла, который доделывает работу после выхода программы.
APPLY_BAT = 'apply_update.bat'

# Лог bat-файла. Лежит рядом с временной папкой, а не внутри неё: папку bat
# в конце удаляет, а открытый файл удалить не даст.
APPLY_LOG_NAME = 'sc_localizer_update.log'


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

    # Без api.github.com: тег — по переадресации /releases/latest, архив —
    # со списка файлов релиза. Подробности в шапке github_source.
    tag = latest_release_tag(UPDATE_REPO)
    if not tag:
        log.info('В %s пока нет релизов программы', UPDATE_REPO)
        return None

    url, date = release_zip(UPDATE_REPO, tag)
    if not url:
        log.warning('В релизе %s нет zip-архива, обновляться нечем', tag)
        return None

    return AppRelease(
        version=tag.lstrip('vV'),
        tag=tag,
        # Описание интерфейс не показывает, а ради него пришлось бы
        # разбирать страницу релиза.
        notes='',
        url=url,
        # Точного размера на странице нет. Не беда: оборванный архив не
        # распакуется (zip проверяет контрольные суммы), и обновление
        # остановится раньше, чем тронет рабочие файлы.
        size=0,
        date=date,
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


# Сообщения в bat-файле латиницей: писать их в cp866 ради одного редкого
# случая не стоит, а кракозябры в момент поломки помогают меньше всего.
#
# /E — со всеми подпапками, /IS /IT — перезаписывать даже совпадающие файлы.
# Без /MIR robocopy ничего лишнего не удаляет, поэтому настройки, cache/
# и output/ у человека остаются на месте.
#
# Две вещи, на которых обновление уже один раз сломалось молча:
#
# 1. Пауза сделана через ping, а не timeout: timeout требует настоящую консоль
#    и без неё падает с 'Input redirection is not supported'.
# 2. Конвейер `tasklist | find` тоже требует рабочих стандартных потоков.
#    Запущенный совсем без них (DETACHED_PROCESS и никакого stdout) bat умирал
#    на первой же строке цикла — файлы не копировались, программа не
#    возвращалась. Поэтому запускаем с CREATE_NO_WINDOW (консоль есть, но
#    скрыта) и отдаём вывод в лог-файл, а не в никуда.
#
# 3. find зовём по полному пути. Если у человека стоит Git для Windows, его
#    Unix-ный find оказывается в PATH раньше системного, и цикл ожидания
#    вместо процессов начинает обходить файлы на дисках.
#
# Плюс robocopy получает щедрые повторы: если exe ещё не успел отпустить свои
# файлы, она дождётся сама, даже если цикл ожидания почему-то отработал рано.
#
# Флаг --updated говорит новой копии не открывать вкладку браузера: старая
# страница сама дождётся ответа сервера и перезагрузится.
_BAT_TEMPLATE = r'''@echo off
cd /d "%~dp0"

set TRIES=0
:wait
tasklist /FI "IMAGENAME eq {exe}" /NH | "%SystemRoot%\System32\find.exe" /I "{exe}"
if errorlevel 1 goto copyfiles
set /a TRIES+=1
if %TRIES% GEQ 60 goto giveup
ping -n 2 127.0.0.1 >nul
goto wait

:copyfiles
robocopy "{src}" "{dst}" /E /IS /IT /R:20 /W:1 /NFL /NDL /NJH /NJS
if errorlevel 8 goto giveup

set "RUNDIR={dst}"
set "MOVEDFROM="
{rename}
:run
start "" /D "%RUNDIR%" "%RUNDIR%{sep}{exe}" --updated %MOVEDFROM%
cd /d "%TEMP%"
rmdir /S /Q "{tmp}" >nul 2>&1
exit /b 0

:giveup
rem Без программы человека не оставляем: поднимаем ту, что была.
rem Она запускается без --updated, чтобы открылось окно и стало видно,
rem что всё живо, а рядом лежит записка с причиной.
> "{dst}{sep}UPDATE_FAILED.txt" echo SC Localizer: update failed. New version is in {src}
start "" /D "{dst}" "{dst}{sep}{exe}"
exit /b 1
'''


# Архив обычно распаковывают «Извлечь всё», и Windows называет папку по имени
# архива: SC_Localizer_v1.4.1\SC_Localizer\. Программа обновляет себя сама,
# и версия в имени папки быстро начинает врать — при обновлении её убираем.
_VERSIONED_DIR_RX = re.compile(r'^SC[_ -]?Localizer[_ -]v?\d+(?:\.\d+)+$', re.I)
PLAIN_DIR_NAME = 'SC_Localizer'
MOVED_FROM_FLAG = '--moved-from'

# Переименование сразу после выхода программы иногда не проходит: Проводник
# или антивирус ещё держат папку. Пробуем несколько раз с паузой.
_RENAME_BLOCK = r'''set TRIES=0
:rename
ren "{old}" "{name}" >nul 2>&1
if not errorlevel 1 (
  set "RUNDIR={new_dst}"
  set MOVEDFROM={flag} "{old}"
  goto run
)
set /a TRIES+=1
if %TRIES% GEQ 5 goto run
ping -n 2 127.0.0.1 >nul
goto rename'''


def versioned_folder(app_dir: Path) -> tuple[Path, Path] | None:
    """
    Папка с версией в имени и куда её переименовать; None — переименовывать нечего.

    Смотрим саму папку программы и ту, что над ней: «Извлечь всё» кладёт
    программу в SC_Localizer_vX\\SC_Localizer\\, но бывает и без вложенности.
    Трогаем только имена, похожие на имя нашего архива, — чужие папки никогда.
    Новое имя уже занято (например, второй копией) — оставляем как есть.
    """
    for folder in (app_dir, app_dir.parent):
        if _VERSIONED_DIR_RX.match(folder.name):
            plain = folder.with_name(PLAIN_DIR_NAME)
            return None if plain.exists() else (folder, plain)
    return None


def _rename_block(app_dir: Path) -> str:
    """Кусок bat, убирающий версию из имени папки; пустой — если нечего."""
    found = versioned_folder(app_dir)
    if not found:
        return ''
    old, plain = found
    return _RENAME_BLOCK.format(old=old, name=plain.name, flag=MOVED_FROM_FLAG,
                                new_dst=plain / app_dir.relative_to(old))


# Где искать ярлыки на программу: рабочий стол, «Пуск», закреплённые на панели
# задач. Рабочий стол без подпапок — там бывают гигабайты чужих файлов.
_SHORTCUT_SCRIPT = r'''
$old = '{old}'; $new = '{new}'
$dirs = @()
{dirs}
$sh = New-Object -ComObject WScript.Shell
$n = 0
foreach ($d in $dirs) {{
  if (-not $d[0] -or -not (Test-Path -LiteralPath $d[0])) {{ continue }}
  $items = if ($d[1]) {{ Get-ChildItem -LiteralPath $d[0] -Filter *.lnk -Recurse -ErrorAction SilentlyContinue }}
           else {{ Get-ChildItem -LiteralPath $d[0] -Filter *.lnk -ErrorAction SilentlyContinue }}
  foreach ($f in $items) {{
    $l = $sh.CreateShortcut($f.FullName)
    if (-not $l.TargetPath.StartsWith($old + '\', [StringComparison]::OrdinalIgnoreCase)) {{ continue }}
    $l.TargetPath = $new + $l.TargetPath.Substring($old.Length)
    if ($l.WorkingDirectory.StartsWith($old, [StringComparison]::OrdinalIgnoreCase)) {{
      $l.WorkingDirectory = $new + $l.WorkingDirectory.Substring($old.Length) }}
    if ($l.IconLocation.StartsWith($old, [StringComparison]::OrdinalIgnoreCase)) {{
      $l.IconLocation = $new + $l.IconLocation.Substring($old.Length) }}
    $l.Save(); $n++
  }}
}}
$n
'''

_DEFAULT_SHORTCUT_DIRS = (
    "@([Environment]::GetFolderPath('Desktop'), $false)",
    "@([Environment]::GetFolderPath('CommonDesktopDirectory'), $false)",
    "@([Environment]::GetFolderPath('Programs'), $true)",
    "@([Environment]::GetFolderPath('CommonPrograms'), $true)",
    "@(\"$env:APPDATA\\Microsoft\\Internet Explorer\\Quick Launch\\User Pinned\\TaskBar\", $false)",
)


def repoint_shortcuts(old: Path, new: Path, folders: list[Path] | None = None) -> int:
    """
    Перенаправляет ярлыки, смотревшие внутрь переименованной папки. Сколько поправлено.

    folders — где искать (для проверки); по умолчанию рабочий стол, «Пуск»
    и закреплённые на панели задач.
    """
    def ps(s: str) -> str:
        return str(s).replace("'", "''")

    # Каждую папку добавляем отдельно: массив из одного элемента PowerShell
    # «разворачивает», и при одной папке поиска перебор бы сломался.
    if folders is None:
        dirs = '\n'.join(f'$dirs += ,{d}' for d in _DEFAULT_SHORTCUT_DIRS)
    else:
        dirs = '\n'.join(f"$dirs += ,@('{ps(f)}', $false)" for f in folders)
    script = _SHORTCUT_SCRIPT.format(old=ps(old), new=ps(new), dirs=dirs)
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                            'Bypass', '-EncodedCommand', encoded],
                           capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        out = r.stdout.decode('utf-8', errors='replace').strip().splitlines()
        return int(out[-1]) if out and out[-1].strip().isdigit() else 0
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.warning('Не удалось поправить ярлыки: %s', e)
        return 0


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
                             tmp=str(tmp), sep=os.sep, rename=_rename_block(target)),
        encoding='cp866', errors='replace')

    # CREATE_NO_WINDOW, а не DETACHED_PROCESS: чёрного окна так же нет, но
    # консоль у процесса есть, и cmd-конвейеры работают. С DETACHED_PROCESS
    # bat умирал молча на первой же команде с конвейером.
    #
    # Вывод пишем в файл: без него не разобрать, почему обновление не доехало,
    # а окна, куда посмотреть, у нас нет. Файл открываем и не закрываем —
    # программа сейчас выйдет, и дескриптор достанется bat-у.
    apply_log = Path(tempfile.gettempdir()) / APPLY_LOG_NAME
    out = open(apply_log, 'wb')
    subprocess.Popen(
        ['cmd', '/c', str(bat)],
        cwd=str(tmp),
        stdout=out, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    log.info('Запущен %s, жду выхода программы. Его лог: %s', bat, apply_log)
