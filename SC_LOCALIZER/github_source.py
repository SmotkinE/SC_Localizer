"""
Загрузка исходных файлов с GitHub.

Английский — StarStrings от MrKraken: обычный английский global.ini, в который
дописаны пулы чертежей в описания контрактов. Релизов там нет, только ветка
master, поэтому версия определяется по коммиту.

Русский — StarCitizenRu от n1ghter. Файлы лежат в дереве репозитория, а к
каждому релизу приложен index.txt с размером и MD5 — берём его и проверяем
скачанное.

К api.github.com программа не обращается вовсе. Без токена он даёт 60
запросов в час на интернет-адрес, и их делят все: браузер на страницах
GitHub, другие программы, соседи за общим адресом провайдера. Программа
упиралась в чужой расход. Всё нужное GitHub отдаёт и обычными страницами:
список релизов — лентой releases.atom, последний релиз — переадресацией
/releases/latest, файлы — с raw.githubusercontent.com и ссылками релизов.
"""
import base64
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import requests

from installer import tag_fits_game
from logger import get_logger

log = get_logger(__name__)

# --- Английский: StarStrings ---
EN_REPO = 'MrKraken/StarStrings'
EN_BRANCH = 'master'
# Где лежит английский. Автор уже переносил файл (Data/... -> src/For_Players/
# Data/...), поэтому пробуем по очереди: текущий путь, потом прежний. Искать
# по дереву репозитория без API нечем — переедет снова, путь допишется сюда.
EN_PATHS = ('src/For_Players/Data/Localization/english/global.ini',
            'Data/Localization/english/global.ini')

# --- Русский: StarCitizenRu ---
RU_REPO = 'n1ghter/StarCitizenRu'
RU_PATH = 'data/Localization/korean_(south_korea)/global.ini'
RU_INDEX_ASSET = 'index.txt'

WEB = 'https://github.com'
RAW = 'https://raw.githubusercontent.com'

# Без таймаута запрос однажды повесит программу навсегда.
TIMEOUT = 30
DOWNLOAD_TIMEOUT = 300
RETRIES = 3
RETRY_PAUSE = 2

# Список релизов держим в памяти несколько минут: открытие страницы и сборка
# сразу следом не должны дважды ходить за одним и тем же.
_CACHE_TTL = 300  # 5 минут
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, fetch):
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _CACHE_TTL:
        return hit[1]
    value = fetch()
    _cache[key] = (time.monotonic(), value)
    return value


class GitHubError(Exception):
    """Не удалось получить данные с GitHub."""


def _minutes_word(n: int) -> str:
    """1 минуту, 2 минуты, 5 минут — иначе фраза выглядит машинной."""
    if 11 <= n % 100 <= 14:
        return 'минут'
    tail = n % 10
    if tail == 1:
        return 'минуту'
    if tail in (2, 3, 4):
        return 'минуты'
    return 'минут'


def _rate_limit_hint(response: requests.Response) -> str:
    """
    Через сколько ограничение снимется.

    GitHub кладёт время сброса в тот же ответ, которым отказывает:
    X-RateLimit-Reset — момент сброса часового лимита (unix-время),
    Retry-After — пауза в секундах при коротких ограничениях. Отдельный
    запрос к /rate_limit ради этого не нужен, да и делать его в момент,
    когда запросы кончились, было бы странно.
    """
    left = None
    retry_after = response.headers.get('Retry-After')
    if retry_after:
        try:
            left = int(retry_after)
        except ValueError:
            left = None
    if left is None:
        reset = response.headers.get('X-RateLimit-Reset')
        try:
            left = int(reset) - int(time.time())
        except (TypeError, ValueError):
            left = None

    if left is None:
        return 'Подожди немного и попробуй снова.'
    if left <= 0:
        return 'Ограничение уже должно было сняться, попробуй снова.'
    if left < 60:
        return f'Попробуй снова через {left} с.'
    minutes = (left + 59) // 60          # округляем вверх: лучше подождать лишнее
    return f'Попробуй снова через {minutes} {_minutes_word(minutes)}.'


def http_get(url: str, *, timeout: int = TIMEOUT, stream: bool = False) -> requests.Response:
    """
    GET с повторами.

    Повторяем только сетевые сбои и 5xx: ретраить 404 бессмысленно, файла
    от этого не появится.
    """
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, timeout=timeout, stream=stream)
            if r.status_code >= 500:
                raise GitHubError(f'GitHub ответил {r.status_code}')
            if r.status_code == 404:
                raise GitHubError(f'Не найдено на GitHub: {url}')
            # Обычные страницы GitHub тоже могут попросить подождать — кодом 429.
            if r.status_code == 429 or (r.status_code == 403 and 'rate limit' in r.text.lower()):
                raise GitHubError('GitHub временно ограничил число запросов. '
                                  + _rate_limit_hint(r))
            r.raise_for_status()
            return r
        except (requests.RequestException, GitHubError) as e:
            last = e
            # 404 и лимит запросов повторять незачем.
            if isinstance(e, GitHubError) and ('Не найдено' in str(e) or 'ограничил' in str(e)):
                raise
            if attempt < RETRIES:
                log.warning('Попытка %d/%d не удалась (%s), повтор...', attempt, RETRIES, e)
                time.sleep(RETRY_PAUSE)

    raise GitHubError(f'Не удалось скачать: {last}')


# ---------- английский ----------

@dataclass
class EnglishVersion:
    tag: str        # то, чем помечаем кэш: ETag файла, стабилен пока файл не менялся
    short: str      # короткая метка для показа
    date: str       # дата последнего изменения файла
    path: str       # путь к файлу в репозитории (может переезжать)


def _english_head() -> 'tuple[requests.Response, str]':
    """
    Находит файл английского: HEAD по известным путям на raw. Возвращает
    ответ и путь, по которому файл нашёлся.

    Сбой сети — это сбой сети, а не переезд файла: раньше он отправлял искать
    файл по всему репозиторию и тратил на это запрос.
    """
    for path in EN_PATHS:
        try:
            r = requests.head(f'{RAW}/{EN_REPO}/{EN_BRANCH}/{path}',
                              timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException as e:
            # Наружу отдаём только GitHubError — вызывающие ловят именно его.
            raise GitHubError(f'Не удалось проверить английский файл: {e}') from e
        if r.status_code == 200:
            if path != EN_PATHS[0]:
                log.warning('Английский нашёлся по прежнему пути: %s', path)
            return r, path

    raise GitHubError('В репозитории StarStrings не найден english/global.ini — '
                      'возможно, автор изменил структуру. Укажи файл вручную.')


def english_version() -> EnglishVersion:
    """Версия английского — из заголовков raw-файла (ETag / Last-Modified)."""
    head, path = _english_head()

    etag = (head.headers.get('ETag') or '').strip('"')
    last_mod = head.headers.get('Last-Modified', '')
    date = ''
    if last_mod:
        try:
            from email.utils import parsedate_to_datetime
            date = parsedate_to_datetime(last_mod).strftime('%Y-%m-%d')
        except (TypeError, ValueError):
            date = ''
    return EnglishVersion(tag=etag or last_mod, short=(etag or 'файл')[:10],
                          date=date, path=path)


def download_english(dest: Path, path: str = '') -> int:
    # Путь могли уже найти в english_version — тогда не ищем повторно.
    if not path:
        path = _english_head()[1]
    url = f'{RAW}/{EN_REPO}/{EN_BRANCH}/{path}'
    return download_file(url, dest, expected_size=None, expected_md5=None)


def pick_release(releases: list['Release'], game_version: str = '') -> 'Release | None':
    """
    Какой релиз брать.

    Релизы приходят от новых к старым, поэтому первый подходящий — он же
    свежайший. Если под нашу серию нет ничего, берём самый новый вообще:
    пусть он под следующий патч, это лучше, чем ничего.

    Совпадение считаем по серии, а точное совпадение версии намеренно
    НЕ выделяем в приоритет. Игра называет себя округлённо: и на 4.10.0,
    и на 4.10.1 в манифесте стоит 4.10.0. Поэтому «точное совпадение» всегда
    указывало бы на самый первый релиз серии — на 4.10.1 программа упорно
    ставила перевод 4.10.0-v125 вместо свежего 4.10.1-v126, да ещё и метила
    его как актуальный.
    """
    if not releases:
        return None
    if game_version:
        for r in releases:
            if tag_fits_game(r.tag, game_version):
                return r
    return releases[0]


# ---------- русский ----------

@dataclass
class Release:
    tag: str
    name: str
    date: str
    prerelease: bool


_ATOM = '{http://www.w3.org/2005/Atom}'


def _releases_from_feed(xml: bytes) -> list[Release]:
    """Релизы из ленты releases.atom — от новых к старым, как их показывает GitHub."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise GitHubError(f'GitHub прислал непонятный список релизов: {e}') from e

    releases = []
    for entry in root.iter(f'{_ATOM}entry'):
        link = entry.find(f'{_ATOM}link')
        href = link.get('href', '') if link is not None else ''
        if '/releases/tag/' not in href:
            continue
        releases.append(Release(
            tag=unquote(href.rsplit('/releases/tag/', 1)[1]),
            name=(entry.findtext(f'{_ATOM}title') or '').strip(),
            date=(entry.findtext(f'{_ATOM}updated') or '')[:10],
            # Отметки «предварительный» в ленте нет, а интерфейс её и не показывает.
            prerelease=False,
        ))
    return releases


def russian_releases(limit: int = 15) -> list[Release]:
    """Последние релизы перевода. В ленте их до десяти — для выбора хватает."""
    def fetch():
        return _releases_from_feed(http_get(f'{WEB}/{RU_REPO}/releases.atom').content)
    return _cached(f'releases:{RU_REPO}', fetch)[:limit]


def _parse_index(text: str) -> dict[str, tuple[int, str]]:
    """
    index.txt из релиза: путь:размер:md5_в_base64 на строку.

    Даёт бесплатную проверку целостности — грех не использовать.
    """
    result = {}
    for line in text.splitlines():
        parts = line.strip().rsplit(':', 2)
        if len(parts) != 3:
            continue
        path, size, md5 = parts
        try:
            result[path] = (int(size), md5)
        except ValueError:
            continue
    return result


def russian_index(tag: str) -> dict[str, tuple[int, str]]:
    url = f'https://github.com/{RU_REPO}/releases/download/{tag}/{RU_INDEX_ASSET}'
    try:
        return _parse_index(http_get(url).text)
    except GitHubError as e:
        # Индекс — приятный бонус, а не обязательное условие: без него
        # просто скачаем без проверки, вместо того чтобы упасть.
        log.warning('Индекс релиза %s недоступен (%s), скачаю без проверки', tag, e)
        return {}


def download_russian(tag: str, dest: Path) -> int:
    index = russian_index(tag)
    size, md5 = index.get(RU_PATH, (None, None))
    url = f'{RAW}/{RU_REPO}/{tag}/{RU_PATH}'
    return download_file(url, dest, expected_size=size, expected_md5=md5)


# ---------- релизы программы ----------

def latest_release_tag(repo: str) -> str:
    """
    Тег последнего релиза — по переадресации /releases/latest на страницу этого
    релиза. Пустая строка — релизов пока нет: тогда GitHub ведёт на /releases.
    """
    try:
        r = requests.head(f'{WEB}/{repo}/releases/latest', timeout=TIMEOUT,
                          allow_redirects=False)
    except requests.RequestException as e:
        raise GitHubError(f'Не удалось проверить релизы {repo}: {e}') from e
    if r.status_code == 429:
        raise GitHubError('GitHub временно ограничил число запросов. ' + _rate_limit_hint(r))

    location = r.headers.get('Location', '')
    if '/releases/tag/' not in location:
        return ''
    return unquote(location.rsplit('/releases/tag/', 1)[1])


def release_zip(repo: str, tag: str) -> tuple[str, str]:
    """
    Ссылка на zip-архив релиза и дата его загрузки; ('', '') — архива нет.

    Берём со страницы, которую GitHub подгружает в раздел Assets. Архивы
    исходников («Source code») лежат по другим адресам и сюда не попадают.
    """
    html = http_get(f'{WEB}/{repo}/releases/expanded_assets/{tag}').text
    link = re.search(rf'href="(/{re.escape(repo)}/releases/download/'
                     rf'{re.escape(tag)}/[^"]+?\.zip)"', html, re.I)
    if not link:
        return '', ''
    date = re.search(r'datetime="(\d{4}-\d{2}-\d{2})', html)
    return WEB + link.group(1), (date.group(1) if date else '')


# ---------- скачивание с проверкой ----------

def download_file(url: str, dest: Path, expected_size: int | None,
              expected_md5: str | None) -> int:
    """
    Качает во временный файл, проверяет и только потом подменяет целевой.

    Недокачанный файл на месте рабочего хуже, чем отсутствие файла: программа
    соберёт из обрезка мусор, и никто не поймёт почему.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + '.part')

    log.info('Качаю %s', url)
    r = http_get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)

    digest = hashlib.md5()
    written = 0
    try:
        with tmp.open('wb') as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                digest.update(chunk)
                written += len(chunk)
    except requests.RequestException as e:
        # Обрыв посреди скачивания: убираем огрызок и отдаём понятную ошибку,
        # а не сырой RequestException, который вызывающие не ловят.
        tmp.unlink(missing_ok=True)
        raise GitHubError(f'Связь оборвалась во время скачивания: {e}') from e

    if expected_size is not None and written != expected_size:
        tmp.unlink(missing_ok=True)
        raise GitHubError(
            f'Размер не совпал: скачано {written} байт, ожидалось {expected_size}. '
            'Скорее всего оборвалась связь, попробуй ещё раз.')

    if expected_md5:
        actual = base64.b64encode(digest.digest()).decode()
        if actual != expected_md5:
            tmp.unlink(missing_ok=True)
            raise GitHubError('Контрольная сумма не совпала — файл скачался повреждённым.')
        log.info('MD5 совпал: %s', actual)

    tmp.replace(dest)
    log.info('Сохранено %s (%d байт)', dest.name, written)
    return written
