"""Чтение и запись global.ini."""
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
