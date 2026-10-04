"""Чтение и запись global.ini."""
import hashlib
import re
from pathlib import Path

from logger import get_logger

log = get_logger(__name__)

# Строка вида `  key = value` с сохранением отступов и пробелов вокруг '='
LINE_RX = re.compile(r'^(\s*)([^=]+?)(\s*=\s*)(.*)$')


def read_text(path: Path) -> str:
    """
    Читает ini, определяя кодировку.

    cp1251 декодирует почти любые байты и не падает, поэтому полагаться на
    исключение нельзя — иначе utf-8 файл молча превратится в кракозябру.
    Проверяем utf-8 первым и принимаем результат, только если он валиден.
    """
    raw = path.read_bytes()
    for enc in ('utf-8-sig', 'utf-8'):
        try:
            text = raw.decode(enc)
            log.debug('Файл %s прочитан как %s', path.name, enc)
            return text
        except UnicodeDecodeError:
            pass

    log.warning('Файл %s не является UTF-8, читаем как cp1251', path)
    return raw.decode('cp1251', errors='replace')


def load_overrides(path: Path) -> dict[str, str]:
    """
    Ручные исправления перевода: ключ=текст.

    Ложатся поверх общего перевода и НЕ зависят от категорий: если строка
    здесь есть, она применяется. Смысл в том, чтобы чинить кривые переводы,
    не трогая исходный русский файл и переживая его обновления.
    """
    if not path.is_file():
        return {}

    data: dict[str, str] = {}
    for line in read_text(path).splitlines():
        line = line.lstrip('﻿')
        if not line.strip() or line.lstrip().startswith((';', '#')) or '=' not in line:
            continue
        key, value = line.split('=', 1)
        data[key.strip()] = value

    log.info('Загружено %d ручных исправлений из %s', len(data), path.name)
    return data


# Заготовка личного файла исправлений. Программа кладёт её рядом с exe,
# если файла нет, и дальше никогда его не трогает.
USER_OVERRIDES_TEMPLATE = """; ТВОИ ИСПРАВЛЕНИЯ ПЕРЕВОДА
;
; Строки отсюда ложатся ПОВЕРХ перевода и не зависят от галочек категорий.
; Это твой файл: обновления программы его не трогают. Исправления автора
; уже встроены в программу, копировать их сюда не нужно.
;
; Формат:   Ключ=Текст
; Комментарий — строка, начинающаяся с ; или #
;
; Как найти ключ: возьми английскую фразу из игры и поищи её в английском
; global.ini обычным поиском (Ctrl+F). Слева от = и будет ключ.
;
; ВАЖНО: подстановки вида ~mission(Ship) и %ls обязаны остаться на месте,
; иначе игра покажет мусор. Программа такие строки проверяет и при поломке
; пропускает, оставляя английский.

"""

# Отпечаток overrides.ini, который раздавался в архивах с 1.1.0 по 1.4.4.
# Тогда файл лежал в архиве, и обновление затирало правки игрока. Если у
# человека лежит ровно он, своего там ничего нет, а единственное исправление
# автора теперь встроено — такой файл можно спокойно заменить заготовкой.
# Иначе старая копия навсегда закрепила бы у него ту версию исправления.
_LEGACY_SHIPPED_SHA256 = '346609284a8009bf1b988bb418ae4202f0105a2d997aa68cc6d22f1f352ebe42'


def _fingerprint(text: str) -> str:
    """Отпечаток текста без учёта BOM, переводов строк и пустых краёв."""
    norm = text.lstrip('﻿').replace('\r\n', '\n').strip()
    return hashlib.sha256(norm.encode('utf-8')).hexdigest()


def ensure_user_overrides(path: Path) -> None:
    """
    Кладёт заготовку личного файла исправлений, если его нет.

    Файл, который игрок хоть как-то менял, не трогаем никогда. Заменяем
    только нетронутую копию из старых архивов — в ней нет ничего своего.
    """
    if path.is_file():
        try:
            untouched = _fingerprint(read_text(path)) == _LEGACY_SHIPPED_SHA256
        except OSError as e:
            log.warning('Не удалось прочитать %s: %s', path, e)
            return
        if not untouched:
            return
        log.info('%s — нетронутая копия из старой версии, меняю на заготовку', path.name)

    try:
        # BOM и CRLF — чтобы файл нормально открывался в Блокноте.
        path.write_text(USER_OVERRIDES_TEMPLATE.replace('\n', '\r\n'), encoding='utf-8-sig',
                        newline='')
    except OSError as e:
        log.warning('Не удалось создать %s: %s', path, e)


def load_ini(path: Path) -> dict[str, str]:
    """Возвращает {ключ: значение}. Дубликаты — побеждает последний, как в игре."""
    data: dict[str, str] = {}
    duplicates = 0

    for line in read_text(path).splitlines():
        line = line.lstrip('﻿')
        if not line or line.startswith('[') or line.startswith(';') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if key in data:
            duplicates += 1
        data[key] = value

    log.info('Загружено %d ключей из %s (дубликатов: %d)', len(data), path.name, duplicates)
    return data
