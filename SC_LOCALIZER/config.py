"""Конфигурация проекта. Значения читаются из .env, не-секреты лежат здесь же."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _app_dir() -> Path:
    """
    Папка, рядом с которой живут данные: output, logs, настройки.

    В собранном exe класть их внутрь упаковки нельзя — там временная папка
    и права только на чтение. Поэтому рядом с самим exe.
    """
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resource_dir() -> Path:
    """Папка с упакованными ресурсами (templates). В сборке это _internal."""
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
    return Path(__file__).resolve().parent


BASE_DIR = _app_dir()
RESOURCE_DIR = _resource_dir()

load_dotenv(BASE_DIR / '.env')


def _path_from_env(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    return Path(raw) if raw else default


class Config:
    # Папки. Дублируются в классе намеренно: код обращается к путям через Config,
    # и без этих строк Config.BASE_DIR падает с AttributeError.
    BASE_DIR = BASE_DIR
    RESOURCE_DIR = RESOURCE_DIR

    IS_FROZEN = getattr(sys, 'frozen', False)

    # Flask
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
    HOST = os.getenv('HOST', '127.0.0.1')
    PORT = int(os.getenv('PORT', 5000))

    # Исходные файлы локализации.
    # Значения по умолчанию — для запуска из исходников рядом со старым проектом.
    # В раздаваемой сборке их не будет, и пользователь выберет файлы кнопкой «Обзор».
    ENGLISH_INI = _path_from_env('ENGLISH_INI', BASE_DIR.parent / 'english' / 'global.ini')
    RUSSIAN_INI = _path_from_env('RUSSIAN_INI', BASE_DIR.parent / 'russian' / 'global.ini')
    OUTPUT_DIR = _path_from_env('OUTPUT_DIR', BASE_DIR / 'output')

    # Логи
    LOG_DIR = BASE_DIR / 'logs'
    LOG_FILE = LOG_DIR / 'sc_localizer.log'
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

    # Сколько строк отдавать в одной странице таблицы ключей
    PAGE_SIZE = int(os.getenv('PAGE_SIZE', 100))

    # Профиль правил (какие категории включены) — сохраняется между запусками
    PROFILE_FILE = BASE_DIR / 'profile.json'

    # Выбранные в интерфейсе пути к файлам — тоже переживают перезапуск
    PATHS_FILE = BASE_DIR / 'paths.json'

    # Ручные правки перевода, ложатся поверх общего русского файла
    OVERRIDES_FILE = BASE_DIR / 'overrides.ini'

    # Сюда качаются файлы с GitHub
    CACHE_DIR = BASE_DIR / 'cache'

    # Какие версии лежат в кэше — чтобы не качать одно и то же заново
    CACHE_META = BASE_DIR / 'cache' / 'versions.json'
