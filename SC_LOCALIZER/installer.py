"""
Установка собранной локализации в игру.

Русского языка в Star Citizen нет, поэтому подменяется корейская локаль:
файл кладётся в korean_(south_korea), а в user.cfg игре говорится
использовать этот язык с английской озвучкой.
"""
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from logger import get_logger

log = get_logger(__name__)

# Подменяемая локаль. Именно эту папку читает игра, когда в user.cfg
# стоит g_language = korean_(south_korea).
LOCALE_DIR = 'korean_(south_korea)'

# Ветки установки. У игрока может быть несколько сразу, и версии в них разные.
KNOWN_BRANCHES = ('LIVE', 'PTU', 'EPTU', 'HOTFIX', 'TECH-PREVIEW')

CFG_LANGUAGE_LINE = f'g_language = {LOCALE_DIR}'
CFG_AUDIO_LINE = 'g_languageAudio = english'

# Строка вида "g_language = korean_(south_korea)" с любыми пробелами
_CFG_LANG_RX = re.compile(r'^\s*g_language\s*=\s*(\S+)', re.I | re.M)


def find_game_dirs() -> list[Path]:
    """Ищет папки StarCitizen по типовым местам на всех дисках."""
    candidates: list[Path] = []
    for letter in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
        root = Path(f'{letter}:\\')
        if not root.exists():
            continue
        for rel in ('Program Files\\Roberts Space Industries\\StarCitizen',
                    'Roberts Space Industries\\StarCitizen',
                    'StarCitizen',
                    'Games\\StarCitizen'):
            p = root / rel
            if p.is_dir() and find_branches(p):
                candidates.append(p)
    return candidates


def find_branches(game_dir: Path) -> list[str]:
    """
    Какие ветки установлены внутри папки StarCitizen.

    Ветку узнаём по наличию data-папки или Data.p4k — иначе в список попадут
    случайные подпапки вроде logs или screenshots.
    """
    if not game_dir.is_dir():
        return []

    found = []
    for child in sorted(game_dir.iterdir()):
        if not child.is_dir():
            continue
        looks_like_branch = (child / 'Data.p4k').is_file() or (child / 'data').is_dir()
        if looks_like_branch and child.name.upper() in KNOWN_BRANCHES:
            found.append(child.name)
    return found


@dataclass
class GameVersion:
    """Версия игры из build_manifest.id."""
    branch: str = ''      # sc-alpha-4.9.0
    version: str = ''     # 4.9.186.58667
    short: str = ''       # 4.9.0 — из ветки; номер патча в ней не растёт, верна только серия
    date: str = ''
    changelist: str = ''  # 12660092 — номер сборки, уникален для каждого патча


# Из "sc-alpha-4.9.0" достаём "4.9.0"
_BRANCH_VER_RX = re.compile(r'(\d+\.\d+\.\d+)')

# Мажор и минор в начале версии: "4.10.1" -> "4.10"
_SERIES_RX = re.compile(r'^(\d+\.\d+)')

# Версия в начале тега релиза: "4.10.1-v126" -> "4.10.1"
_TAG_VER_RX = re.compile(r'^(\d+\.\d+\.\d+)-')


def version_series(version: str) -> str:
    """Серия патча: '4.10.1' -> '4.10'. Пустая строка, если версия не разобралась."""
    m = _SERIES_RX.match(version.strip())
    return m.group(1) if m else ''


# Ветки с обычной игрой. Остальные (PTU, EPTU, TECH-PREVIEW) — тестовые сервера.
LIVE_BRANCHES = ('LIVE', 'HOTFIX')

# Сборки перевода для тестового сервера помечены хвостом: 4.10.0-v124-ptu.
_PTU_TAG_RX = re.compile(r'-e?ptu\b', re.I)


def is_ptu_tag(tag: str) -> bool:
    """Сборка перевода для тестового сервера (PTU/EPTU)."""
    return bool(_PTU_TAG_RX.search(tag or ''))


def is_live_branch(branch: str) -> bool:
    """Обычная игра. Ветка неизвестна — считаем обычной: так играет почти каждый."""
    return (branch or 'LIVE').upper() in LIVE_BRANCHES


def tag_fits_game(tag: str, game_version: str, branch: str = '') -> bool:
    """
    Подходит ли релиз перевода установленной игре.

    Сравниваем по серии (4.10), а не по полной версии, потому что игра сама
    называет себя неточно: в build_manifest.id у патча 4.10.1 ветка осталась
    sc-alpha-4.10.0, и строгое сравнение отвергало заведомо правильный перевод
    с тегом 4.10.1-v126. Внутри одной серии тексты расходятся на считанные
    строки, а вот 4.10 против 4.11 — уже другой патч, и такой релиз мы
    по-прежнему считаем неподходящим.

    Обычной игре сборка для тестового сервера не подходит никогда: она под
    другой билд, и в одной серии с LIVE оказывается регулярно. Тестовому
    серверу годится любая сборка серии — свежая LIVE лучше устаревшей PTU.
    """
    if not tag or not game_version:
        return False
    if is_live_branch(branch) and is_ptu_tag(tag):
        return False

    m = _TAG_VER_RX.match(tag.strip())
    if not m:
        return False

    series = version_series(game_version)
    return bool(series) and version_series(m.group(1)) == series


def game_version(branch_dir: Path) -> GameVersion | None:
    """
    Версия установленной игры.

    Нужна, чтобы сверять с версией перевода: релизы русского помечены
    тегом вида 4.9.0-v112. Сверяет tag_fits_game — по серии патча, потому
    что сама игра свою версию в манифесте округляет.
    """
    manifest = branch_dir / 'build_manifest.id'
    if not manifest.is_file():
        return None

    try:
        data = json.loads(manifest.read_text(encoding='utf-8', errors='replace')).get('Data', {})
    except (json.JSONDecodeError, OSError) as e:
        log.warning('Не удалось прочитать build_manifest.id: %s', e)
        return None

    branch = data.get('Branch', '')
    m = _BRANCH_VER_RX.search(branch)
    return GameVersion(
        branch=branch,
        version=data.get('Version', ''),
        short=m.group(1) if m else '',
        date=data.get('BuildDateStamp', ''),
        changelist=str(data.get('RequestedP4ChangeNum', '')).strip(),
    )


# Строка сборки, которую MrKraken пишет в свой английский файл:
# sc-alpha-4.10.1_live_12660092 — версия патча, канал, номер сборки.
_STARSTRINGS_BUILD_RX = re.compile(r'sc-alpha-(\d+\.\d+\.\d+)_[a-z]+_(\d+)', re.I)
_build_cache: dict[tuple[str, float], tuple[str, str] | None] = {}


def starstrings_build(english_ini: Path) -> tuple[str, str] | None:
    """(версия патча, номер сборки), под которые собран английский StarStrings."""
    try:
        key = (str(english_ini), english_ini.stat().st_mtime)
    except OSError:
        return None
    if key not in _build_cache:
        try:
            m = _STARSTRINGS_BUILD_RX.search(english_ini.read_text(encoding='utf-8', errors='replace'))
        except OSError:
            m = None
        _build_cache[key] = (m.group(1), m.group(2)) if m else None
    return _build_cache[key]


def shown_version(game: GameVersion, english_ini: Path | None) -> str:
    """
    Версия игры, которую не стыдно показать человеку.

    В манифесте игры номер патча не растёт: и 4.10.0, и 4.10.1 называют себя
    4.10.0 — а лаунчер и все вокруг говорят 4.10.1. Точный патч знает MrKraken:
    в английский файл он пишет строку сборки с её номером. Номер совпал с
    номером сборки игры — значит, и патч тот же. Не совпал (игра обновилась
    раньше StarStrings) — честно показываем только серию, 4.10.
    """
    build = starstrings_build(english_ini) if english_ini else None
    if build and game.changelist and build[1] == game.changelist:
        return build[0]
    return version_series(game.short)


@dataclass
class InstallResult:
    ok: bool = True
    installed_to: str = ''
    backup: str = ''
    # Что случилось с user.cfg: created | already_ok | needs_manual | conflict
    cfg_status: str = ''
    cfg_message: str = ''
    messages: list[str] = field(default_factory=list)


# Сколько бэкапов держать. Каждый — ~15 МБ, и без предела за месяц обновлений
# в папке игры молча накопились бы сотни мегабайт.
_BACKUPS_KEEP = 3


def _backup_existing(target: Path) -> str:
    """
    Прячет прежний файл перед заменой.

    У человека там может лежать настоящая корейская локализация или прошлая
    версия перевода — затирать без возможности вернуть нельзя.
    """
    if not target.is_file():
        return ''
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = target.with_name(f'{target.name}.backup-{stamp}')
    shutil.copy2(target, backup)
    log.info('Прежний файл сохранён: %s', backup)

    # Старые бэкапы сверх лимита убираем: имена сортируются по дате,
    # свежие — в конце списка.
    backups = sorted(target.parent.glob(f'{target.name}.backup-*'))
    for old in backups[:-_BACKUPS_KEEP]:
        try:
            old.unlink()
            log.info('Убран старый бэкап: %s', old.name)
        except OSError as e:
            log.warning('Не удалось убрать бэкап %s: %s', old.name, e)

    return str(backup)


def _handle_user_cfg(branch_dir: Path) -> tuple[str, str]:
    """
    Приводит user.cfg в порядок. Возвращает (статус, сообщение).

    Существующий файл не переписываем: там могут быть чужие настройки,
    а язык — осознанный выбор игрока. Молча менять его нельзя.
    """
    cfg = branch_dir / 'user.cfg'

    if not cfg.is_file():
        cfg.write_text(f'{CFG_LANGUAGE_LINE}\n{CFG_AUDIO_LINE}\n', encoding='utf-8')
        log.info('Создан user.cfg: %s', cfg)
        return 'created', 'Файл user.cfg создан, язык прописан.'

    try:
        text = cfg.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        log.error('Не удалось прочитать user.cfg: %s', e, exc_info=True)
        return 'needs_manual', f'Не удалось прочитать user.cfg. Впиши строку сам: {CFG_LANGUAGE_LINE}'

    m = _CFG_LANG_RX.search(text)
    if m is None:
        return 'needs_manual', (
            'user.cfg уже есть, но строки языка в нём нет. '
            f'Открой его и добавь: {CFG_LANGUAGE_LINE}'
        )

    current = m.group(1).strip()
    if current.lower() == LOCALE_DIR:
        return 'already_ok', 'user.cfg уже настроен, менять нечего.'

    # 'english' — родной язык игры и наш же дефолт (его ставит кнопка
    # «только английский»). Это не осознанный выбор чужого языка, поэтому
    # спокойно переключаем на русский. Иначе после «только английский»
    # нельзя было бы вернуться на перевод — язык застревал на english.
    if current.lower() == 'english':
        new_text = _CFG_LANG_RX.sub(CFG_LANGUAGE_LINE, text)
        cfg.write_text(new_text, encoding='utf-8')
        log.info('user.cfg: english -> %s', LOCALE_DIR)
        return 'created', 'user.cfg: язык переключён на русский.'

    return 'conflict', (
        f'В user.cfg стоит другой язык: g_language = {current}. '
        f'Не трогаю его. Если нужен русский, замени строку на: {CFG_LANGUAGE_LINE}'
    )


def install_english(branch_dir: Path, source_ini: Path) -> InstallResult:
    """
    Ставит чистый английский StarStrings — английский текст с блюпринтами
    в описаниях контрактов, без русского перевода.

    Кладётся в english-локаль (переопределяет оригинал игры), язык остаётся
    английским. В user.cfg прописываем english, чтобы игра точно читала его,
    а не оставалась на «корейском» после прошлой установки перевода.
    """
    result = InstallResult()

    if not source_ini.is_file():
        result.ok = False
        result.messages.append(f'Нет английского файла: {source_ini}')
        return result
    if not branch_dir.is_dir():
        result.ok = False
        result.messages.append(f'Папка ветки не найдена: {branch_dir}')
        return result

    locale_dir = branch_dir / 'data' / 'Localization' / 'english'
    locale_dir.mkdir(parents=True, exist_ok=True)

    target = locale_dir / 'global.ini'
    result.backup = _backup_existing(target)
    if result.backup:
        result.messages.append(f'Прежний файл сохранён: {Path(result.backup).name}')

    shutil.copy2(source_ini, target)
    result.installed_to = str(target)
    result.messages.append(f'Установлен английский с блюпринтами: {target}')
    log.info('Английский StarStrings установлен: %s', target)

    # Возвращаем язык на english — иначе после установки русского игра осталась
    # бы на korean-локали и английский файл не показался бы.
    cfg = branch_dir / 'user.cfg'
    if cfg.is_file():
        text = cfg.read_text(encoding='utf-8', errors='replace')
        new = _CFG_LANG_RX.sub('g_language = english', text)
        if new != text:
            cfg.write_text(new, encoding='utf-8')
            result.messages.append('user.cfg: язык переключён на english')
            result.cfg_status = 'created'
        else:
            result.cfg_status = 'already_ok'
    result.cfg_message = 'Готово: язык — английский с блюпринтами.'
    result.messages.append(result.cfg_message)
    return result


def install(branch_dir: Path, source_ini: Path,
            ) -> InstallResult:
    """
    Кладёт собранный global.ini в игру и настраивает user.cfg.

    Язык прописывается сам, строкой g_language в user.cfg, поэтому в меню
    выбора языка игрок не заходит — и как там подписана корейская локаль,
    значения не имеет.
    """
    result = InstallResult()

    if not source_ini.is_file():
        result.ok = False
        result.messages.append(f'Нет собранного файла: {source_ini}')
        return result

    if not branch_dir.is_dir():
        result.ok = False
        result.messages.append(f'Папка ветки не найдена: {branch_dir}')
        return result

    locale_dir = branch_dir / 'data' / 'Localization' / LOCALE_DIR
    locale_dir.mkdir(parents=True, exist_ok=True)

    target = locale_dir / 'global.ini'
    result.backup = _backup_existing(target)
    if result.backup:
        result.messages.append(f'Прежний файл сохранён: {Path(result.backup).name}')

    shutil.copy2(source_ini, target)
    result.installed_to = str(target)
    result.messages.append(f'Установлено: {target}')
    log.info('Локализация установлена: %s', target)

    result.cfg_status, result.cfg_message = _handle_user_cfg(branch_dir)
    result.messages.append(result.cfg_message)

    return result


def restore_original(branch_dir: Path) -> InstallResult:
    """
    Возвращает игре родной текст: убирает всё, что мы ей подкладывали.

    Нужно, когда вышел новый патч, а перевода под него ещё нет. Тогда любой
    установленный файл — от прошлой версии игры, и часть строк в нём просто
    отсутствует. Оригинал лежит внутри Data.p4k и подставляется сам, как только
    рядом не остаётся наших global.ini, поэтому качать для этого нечего.

    Забираем обе локали: korean_(south_korea) — русский перевод, english —
    английский с блюпринтами. Файлы не стираем совсем, а сохраняем рядом
    бэкапом: вернуться к переводу можно будет одной кнопкой.
    """
    result = InstallResult()

    if not branch_dir.is_dir():
        result.ok = False
        result.messages.append(f'Папка ветки не найдена: {branch_dir}')
        return result

    removed = []
    for locale in (LOCALE_DIR, 'english'):
        target = branch_dir / 'data' / 'Localization' / locale / 'global.ini'
        if not target.is_file():
            continue

        backup = _backup_existing(target)
        try:
            target.unlink()
        except OSError as e:
            log.error('Не удалось убрать %s: %s', target, e, exc_info=True)
            result.ok = False
            result.messages.append(f'Не удалось убрать {target}: {e}')
            return result

        removed.append(locale)
        result.backup = backup
        result.messages.append(f'Убран подменённый global.ini из {locale}')
        log.info('Убран подменённый файл: %s', target)

    if not removed:
        result.messages.append('Подменённых файлов не было — в игре и так оригинал')

    result.cfg_status, result.cfg_message = _restore_user_cfg(branch_dir)
    result.messages.append(result.cfg_message)
    return result


def _restore_user_cfg(branch_dir: Path) -> tuple[str, str]:
    """
    Возвращает в user.cfg английский — родной язык игры.

    Чужой язык (китайский, немецкий) не трогаем: его выбирал человек, а не мы.
    Свои значения — korean-локаль и english — переставляем спокойно.
    """
    cfg = branch_dir / 'user.cfg'
    if not cfg.is_file():
        return 'already_ok', 'Готово: игра показывает свой оригинальный текст.'

    try:
        text = cfg.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        log.error('Не удалось прочитать user.cfg: %s', e, exc_info=True)
        return 'needs_manual', ('Не удалось прочитать user.cfg. '
                                'Открой его и поставь: g_language = english')

    m = _CFG_LANG_RX.search(text)
    if m is None:
        return 'already_ok', 'Готово: игра показывает свой оригинальный текст.'

    current = m.group(1).strip()
    if current.lower() == 'english':
        return 'already_ok', 'Готово: игра показывает свой оригинальный текст.'

    if current.lower() != LOCALE_DIR:
        return 'conflict', (
            f'В user.cfg стоит чужой язык: g_language = {current}. Не трогаю его. '
            'Если нужен оригинал, замени строку на: g_language = english')

    cfg.write_text(_CFG_LANG_RX.sub('g_language = english', text), encoding='utf-8')
    log.info('user.cfg: %s -> english', LOCALE_DIR)
    return 'created', 'Готово: язык вернулся на english, игра показывает оригинал.'


def localization_file(branch_dir: Path, locale: str) -> Path:
    """Куда игра смотрит за текстом этой локали."""
    return branch_dir / 'data' / 'Localization' / locale / 'global.ini'


def active_language(branch_dir: Path) -> str:
    """
    Язык, который включит игра: g_language из user.cfg. Нет файла или
    строки — игра на english.

    По нему понятно, какой из наших файлов сейчас в деле: после «английского
    с блюпринтами» русский файл остаётся на месте, но игра его уже не читает.
    """
    try:
        text = (branch_dir / 'user.cfg').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return 'english'
    m = _CFG_LANG_RX.search(text)
    return m.group(1).strip().lower() if m else 'english'
