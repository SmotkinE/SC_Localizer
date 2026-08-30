"""
Загрузка исходных файлов с GitHub.

Английский — StarStrings от MrKraken: обычный английский global.ini, в который
дописаны пулы чертежей в описания контрактов. Релизов там нет, только ветка
master, поэтому версия определяется по коммиту.

Русский — StarCitizenRu от n1ghter. Файлы лежат в дереве репозитория, а к
каждому релизу приложен index.txt с размером и MD5 — берём его и проверяем
скачанное.
"""
import base64
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from logger import get_logger

log = get_logger(__name__)

# --- Английский: StarStrings ---
EN_REPO = 'MrKraken/StarStrings'
EN_BRANCH = 'master'
# Путь-подсказка. Автор уже переносил файл (Data/... -> src/For_Players/Data/...),
# поэтому не полагаемся на него: если по нему 404, ищем в дереве репозитория.
EN_PATH_HINT = 'src/For_Players/Data/Localization/english/global.ini'
EN_PATH_SUFFIX = 'english/global.ini'

# --- Русский: StarCitizenRu ---
RU_REPO = 'n1ghter/StarCitizenRu'
RU_PATH = 'data/Localization/korean_(south_korea)/global.ini'
RU_INDEX_ASSET = 'index.txt'

API = 'https://api.github.com'
RAW = 'https://raw.githubusercontent.com'

# Без таймаута запрос однажды повесит программу навсегда.
TIMEOUT = 30
DOWNLOAD_TIMEOUT = 300
RETRIES = 3
RETRY_PAUSE = 2

# GitHub без токена даёт всего 60 запросов в час на IP — и только к api.github.com.
# Запросы к raw.githubusercontent.com в лимит не входят. Поэтому:
#  - версию английского берём через HEAD на raw (0 к лимиту), а не через commits API;
#  - список релизов (единственный неизбежный API-вызов) кэшируем в памяти,
#    чтобы открытие страницы и последующая сборка не тратили по запросу каждый.
_API_CACHE_TTL = 300  # 5 минут
_api_cache: dict[str, tuple[float, object]] = {}


def _cached_api(key: str, fetch):
    hit = _api_cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _API_CACHE_TTL:
        return hit[1]
    value = fetch()
    _api_cache[key] = (time.monotonic(), value)
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
            r = requests.get(url, timeout=timeout, stream=stream,
                             headers={'Accept': 'application/vnd.github+json'})
            if r.status_code >= 500:
                raise GitHubError(f'GitHub ответил {r.status_code}')
            if r.status_code == 404:
                raise GitHubError(f'Не найдено на GitHub: {url}')
            if r.status_code == 403 and 'rate limit' in r.text.lower():
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


def _english_head() -> 'requests.Response | None':
    """
    HEAD на файл английского по известному пути. Не тратит лимит API.

    Возвращает ответ, если файл на месте, иначе None — тогда путь ищем в дереве.
    """
    try:
        r = requests.head(f'{RAW}/{EN_REPO}/{EN_BRANCH}/{EN_PATH_HINT}',
                         timeout=TIMEOUT, allow_redirects=True)
        return r if r.status_code == 200 else None
    except requests.RequestException:
        return None


def english_version() -> EnglishVersion:
    """
    Версия английского — из заголовков raw-файла (ETag / Last-Modified).

    Через raw, а не через commits API: последнее тратило бы драгоценный запрос
    из лимита в 60/час на каждое открытие страницы и каждую сборку.
    """
    head = _english_head()
    path = EN_PATH_HINT
    if head is None:
        # Файл переехал — ищем в дереве (один запрос к API, редкий случай).
        path = _find_english_path()
        try:
            head = requests.head(f'{RAW}/{EN_REPO}/{EN_BRANCH}/{path}',
                                 timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException as e:
            # Наружу отдаём только GitHubError — вызывающие ловят именно его.
            raise GitHubError(f'Не удалось проверить английский файл: {e}') from e

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


def _find_english_path() -> str:
    """
    Ищет english/global.ini в дереве репозитория — на случай, если файл переехал.

    Один запрос к API, поэтому зовётся только когда файл не по обычному пути.
    """
    log.warning('Английский не по обычному пути, ищу в дереве репозитория')
    r = http_get(f'{API}/repos/{EN_REPO}/git/trees/{EN_BRANCH}?recursive=1')
    for item in r.json().get('tree', []):
        if item.get('path', '').lower().endswith(EN_PATH_SUFFIX):
            log.info('Английский найден: %s', item['path'])
            return item['path']

    raise GitHubError('В репозитории StarStrings не найден english/global.ini — '
                      'возможно, автор изменил структуру. Укажи файл вручную.')


def download_english(dest: Path, path: str = '') -> int:
    # Путь могли уже найти в english_version — тогда не ищем повторно.
    if not path:
        path = EN_PATH_HINT if _english_head() else _find_english_path()
    url = f'{RAW}/{EN_REPO}/{EN_BRANCH}/{path}'
    return download_file(url, dest, expected_size=None, expected_md5=None)


def pick_release(releases: list['Release'], game_version: str = '') -> 'Release | None':
    """
    Какой релиз брать.

    Релизы приходят от новых к старым. Если версия игры известна — берём
    свежайший под неё: самый новый вообще может быть уже под следующий патч,
    и тогда часть строк останется без перевода.
    """
    if not releases:
        return None
    if game_version:
        for r in releases:
            if r.tag.startswith(game_version + '-'):
                return r
    return releases[0]


# ---------- русский ----------

@dataclass
class Release:
    tag: str
    name: str
    date: str
    prerelease: bool


def russian_releases(limit: int = 15) -> list[Release]:
    # Кэшируем: это единственный неизбежный запрос к API, и без кэша открытие
    # страницы плюс сборка тратили бы его дважды подряд.
    def fetch():
        r = http_get(f'{API}/repos/{RU_REPO}/releases?per_page={limit}')
        return [
            Release(
                tag=x.get('tag_name', ''),
                name=x.get('name', ''),
                date=(x.get('published_at') or '')[:10],
                prerelease=bool(x.get('prerelease')),
            )
            for x in r.json()
        ]
    return _cached_api(f'releases:{limit}', fetch)


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
