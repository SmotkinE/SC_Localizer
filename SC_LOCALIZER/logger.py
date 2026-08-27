"""Настройка логирования с ротацией."""
import logging
import sys
from logging.handlers import RotatingFileHandler

from config import Config

_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(Config.LOG_LEVEL)
    formatter = logging.Formatter(_FORMAT)

    Config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        Config.LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Под pythonw.exe (запуск без консоли) sys.stderr равен None, и StreamHandler
    # на нём падает при первой же записи в лог. Файловый лог при этом работает.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)

    return logger
