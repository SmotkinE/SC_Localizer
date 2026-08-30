"""
Веб-интерфейс для сборки русифицированного global.ini.

Запуск:  python app.py   ->  http://127.0.0.1:5000
Или просто двойной клик по ЗАПУСТИТЬ.bat
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import HTTPException

from config import Config
from file_dialog import run_dialog_to_file
from github_source import (EN_BRANCH, GitHubError, download_english, download_russian,
                           english_version, pick_release, russian_releases)
from ini_io import load_ini, load_overrides
from installer import find_branches, find_game_dirs, game_version, install, install_english
from logger import get_logger
from merger import merge
from rules import (CATEGORIES, DEFAULTS_VERSION, ENGLISH_ID, FULL_ID, classify,
                   default_profile, defaults_changed_since)
from updater import (UPDATE_REPO, AppRelease, UpdateError, apply_update, is_newer,
                     latest_release, stage_update, updates_supported)
from version import APP_VERSION

log = get_logger(__name__)

# Шаблоны в собранном exe лежат в упаковке, а не рядом со скриптом,
# поэтому путь задаём явно.
app = Flask(__name__, template_folder=str(Config.RESOURCE_DIR / 'templates'))

# Флаг, которым программа зовёт саму себя, чтобы показать окно выбора файла.
FILE_DIALOG_FLAG = '--file-dialog'

# Флаг, который ставит bat при перезапуске после обновления. Браузер тогда
# не открываем: старая вкладка ждёт ответа сервера и перезагрузится сама,
# а второе окно поверх неё — лишнее.
UPDATED_FLAG = '--updated'

# Файлы на 90 тысяч ключей читаются пару секунд — держим в памяти.
_store: dict = {'en': {}, 'ru': {}, 'en_path': None, 'ru_path': None}

# Лог для окна в интерфейсе. Файловый лог пишется отдельно и живёт в logs/.
_ui_log: deque = deque(maxlen=200)


def ui_log(message: str, level: str = 'info') -> None:
    _ui_log.append({
        'time': datetime.now().strftime('%H:%M:%S'),
        'level': level,
        'text': message,
    })


# ---------- пути к файлам ----------

def _default_ini(path: Path) -> str:
    """
    Путь по умолчанию имеет смысл, только если файл там реально лежит.

    В раздаваемой сборке рядом с exe нет ни english, ни russian, и подстановка
    несуществующего пути выглядела как «файл не найден» на ровном месте.
    """
    return str(path) if path.is_file() else ''


_PATH_DEFAULTS = {
    'english': lambda: _default_ini(Config.ENGLISH_INI),
    'russian': lambda: _default_ini(Config.RUSSIAN_INI),
    'game_dir': lambda: '',
    'branch': lambda: '',
    # Откуда берём файлы: 'github[:<тег>]' или 'manual'.
    # По умолчанию github: свежая установка обязана работать по одной кнопке,
    # без похода в спойлер. С 'manual' здесь первый запуск падал с «файл
    # не найден», потому что качать программа даже не пыталась.
    'source': lambda: 'github',
    'ru_tag': lambda: '',
}


def load_paths() -> dict:
    """Настройки путей переживают перезапуск, иначе выбирать всё пришлось бы заново."""
    saved = {}
    if Config.PATHS_FILE.exists():
        try:
            saved = json.loads(Config.PATHS_FILE.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError) as e:
            log.warning('Не удалось прочитать сохранённые пути: %s', e)

    result = {}
    for key, default in _PATH_DEFAULTS.items():
        value = saved.get(key)
        result[key] = value if value not in (None, '') else default()
    return result


def _mark_manual() -> None:
    """Как только пути правят руками, метка «скачано с GitHub» перестаёт быть правдой."""
    save_paths(source='manual', ru_tag='')


def save_paths(**values) -> None:
    current = load_paths()
    current.update({k: v for k, v in values.items() if k in _PATH_DEFAULTS})
    Config.PATHS_FILE.write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding='utf-8'
    )


# ---------- профиль категорий ----------

# Версия умолчаний, которой записан файл. Профили старых сборок её не имеют —
# считаем такие первой версией.
PROFILE_VERSION_KEY = '_defaults_version'


def load_profile() -> dict[str, bool]:
    if not Config.PROFILE_FILE.exists():
        return default_profile()

    try:
        saved = json.loads(Config.PROFILE_FILE.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as e:
        log.warning('Не удалось прочитать профиль, беру значения по умолчанию: %s', e)
        return default_profile()

    profile = default_profile()
    profile.update({k: bool(v) for k, v in saved.items() if k in profile})

    # Сохранённый выбор перебивает умолчания — иначе настройки сбрасывались бы
    # при каждом обновлении. Но у категорий, которым умолчание меняли уже после
    # записи этого файла, сохранённое значение человек сознательно не выбирал:
    # оно просто досталось от старой сборки. Такие категории один раз
    # выравниваем по новому умолчанию.
    try:
        was = int(saved.get(PROFILE_VERSION_KEY, 1))
    except (TypeError, ValueError):
        was = 1

    changed = defaults_changed_since(was)
    if changed:
        fresh = default_profile()
        for cid in changed:
            if cid in profile:
                profile[cid] = fresh[cid]
        log.info('Профиль был версии %d, обновил умолчания у %s',
                 was, ', '.join(sorted(changed)))
        save_profile(profile)

    return profile


def save_profile(profile: dict[str, bool]) -> None:
    data = dict(profile)
    data[PROFILE_VERSION_KEY] = DEFAULTS_VERSION
    Config.PROFILE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
    )


# ---------- загрузка ini ----------

def ensure_loaded() -> None:
    """Читает файлы, если путь сменился или это первый запрос."""
    paths = load_paths()

    # Пустой путь нельзя отдавать в Path: получается Path('.'), и человек
    # видит бессмысленное «Файл не найден: .» вместо внятной причины.
    if not paths['english'] or not paths['russian']:
        raise FileNotFoundError(
            'Файлы перевода не скачаны. Нажми «Установить в игру» — '
            'программа их достанет сама.')

    en_path, ru_path = Path(paths['english']), Path(paths['russian'])

    if _store['en_path'] == en_path and _store['ru_path'] == ru_path and _store['en']:
        return

    missing = [str(p) for p in (en_path, ru_path) if not p.is_file()]
    if missing:
        raise FileNotFoundError('Файл не найден: ' + '; '.join(missing))

    ui_log(f'Читаю английский: {en_path.name}')
    _store['en'] = load_ini(en_path)
    ui_log(f'Прочитано {len(_store["en"]):,} ключей'.replace(',', ' '))

    ui_log(f'Читаю русский: {ru_path.name}')
    _store['ru'] = load_ini(ru_path)
    ui_log(f'Прочитано {len(_store["ru"]):,} ключей'.replace(',', ' '))

    _store['en_path'], _store['ru_path'] = en_path, ru_path


@app.after_request
def no_cache(response):
    """
    Запрещаем кэширование.

    Без этих заголовков Chrome держит у себя прошлую версию страницы: после
    правки интерфейса пользователь видит старую разметку, её скрипты стучатся
    в исчезнувшие эндпоинты, и кнопки молча перестают работать.
    """
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/favicon.ico')
def favicon():
    """Браузер просит иконку сам. Отдаём пустой ответ, чтобы не плодить 404."""
    return '', 204


def _paths_payload(english: str, russian: str) -> dict:
    return {
        'english': english,
        'russian': russian,
        'english_ok': bool(english) and Path(english).is_file(),
        'russian_ok': bool(russian) and Path(russian).is_file(),
    }


@app.route('/api/paths', methods=['GET', 'POST'])
def api_paths():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        current = load_paths()
        english = (data.get('english') or current['english']).strip()
        russian = (data.get('russian') or current['russian']).strip()

        # Сохраняем как есть, даже если файла нет.
        # Требовать валидности обоих путей сразу нельзя: пользователь выбирает
        # их по одному, и отказ из-за ещё не выбранного второго откатывал первый.
        # Существование проверяется при сборке, а интерфейс показывает статус
        # под каждым полем отдельно.
        changed = (english != current['english']) or (russian != current['russian'])
        save_paths(english=english, russian=russian)

        # В ручной режим уходим, только если путь действительно сменили.
        # Иначе достаточно случайно задеть поле в спойлере — и автообновление
        # молча выключится навсегда, а человек об этом не узнает.
        if changed:
            _mark_manual()
            _startup.update(state='manual', message='Файлы указаны вручную — '
                                                    'обновления не проверяются')
            ui_log('Файлы указаны вручную — автозагрузка с GitHub отключена', 'warn')

        payload = _paths_payload(english, russian)
        if changed:
            ui_log('Пути к файлам сохранены'
                   if payload['english_ok'] and payload['russian_ok']
                   else 'Путь сохранён, второй файл ещё не выбран')
        return jsonify(payload)

    paths = load_paths()
    return jsonify(_paths_payload(paths['english'], paths['russian']))


_BROWSE_TITLES = {
    'english': ('Выберите английскую локализацию', 'file'),
    'russian': ('Выберите русскую локализацию', 'file'),
    'game_dir': ('Выберите папку StarCitizen', 'dir'),
}


@app.route('/api/browse', methods=['POST'])
def api_browse():
    """Открывает нативное окно выбора файла на машине, где крутится сервер."""
    data = request.get_json(silent=True) or {}
    which = data.get('which', 'english')
    title, mode = _BROWSE_TITLES.get(which, _BROWSE_TITLES['english'])

    current = Path(load_paths().get(which) or '')
    if mode == 'dir':
        initial = str(current if current.is_dir() else Config.BASE_DIR)
    else:
        initial = str(current.parent if current.parent.is_dir() else Config.BASE_DIR)

    # Диалог показываем отдельным процессом: обработчики запросов живут в рабочих
    # потоках, а tkinter требует главный поток и из потока подвешивает сервер.
    #
    # В собранном exe sys.executable — это сам exe, а не python. Поэтому там
    # зовём самих себя со скрытым флагом; из исходников — python с file_dialog.py.
    with tempfile.TemporaryDirectory() as tmp:
        out_file = str(Path(tmp) / 'result.txt')
        if Config.IS_FROZEN:
            cmd = [sys.executable, FILE_DIALOG_FLAG, title, initial, out_file, mode]
        else:
            cmd = [sys.executable, str(Config.BASE_DIR / 'file_dialog.py'),
                   title, initial, out_file, mode]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'Окно выбора файла не закрыли за 3 минуты'}), 400

        if result.returncode != 0:
            log.error('Окно выбора файла упало: %s', result.stderr)
            return jsonify({'error': 'Не удалось открыть окно выбора файла'}), 500

        chosen = Path(out_file)
        path = chosen.read_text(encoding='utf-8').strip() if chosen.exists() else ''

    return jsonify({'path': path})


def _autodetect_game() -> tuple[str, list[str], str]:
    """
    Папка игры, список её веток и выбранная ветка.

    Если папку ещё не выбирали — ищем сами. Если сохранённая ветка исчезла
    (игрок удалил PTU) — берём первую доступную. Живёт отдельно от обработчика,
    потому что стартовая проверка обновлений зовёт то же самое: без версии игры
    не выбрать подходящий релиз перевода.
    """
    paths = load_paths()
    game_dir = paths['game_dir']
    if not game_dir:
        found = find_game_dirs()
        if found:
            game_dir = str(found[0])
            save_paths(game_dir=game_dir)
            ui_log(f'Игра найдена автоматически: {game_dir}')

    branches = find_branches(Path(game_dir)) if game_dir else []
    branch = paths['branch']
    if branch not in branches:
        branch = branches[0] if branches else ''
        if branch:
            save_paths(branch=branch)
    return game_dir, branches, branch


def _game_version_short() -> str:
    """Версия игры вида '4.9.0' — с ней сверяются теги релизов перевода."""
    paths = load_paths()
    if not paths['game_dir'] or not paths['branch']:
        return ''
    v = game_version(Path(paths['game_dir']) / paths['branch'])
    return v.short if v else ''


@app.route('/api/source')
def api_source():
    """Что доступно на GitHub и что сейчас используется."""
    paths = load_paths()
    game_ver = _game_version_short()
    result = {
        'source': paths['source'],
        'ru_tag': paths['ru_tag'],
        'english': paths['english'],
        'russian': paths['russian'],
        'game_version': game_ver,
        'releases': [],
        'english_version': None,
        'error': None,
    }

    # Сеть может не работать — программа обязана открыться и без неё,
    # с ручным выбором файлов.
    try:
        result['releases'] = [
            {
                'tag': r.tag, 'date': r.date, 'name': r.name, 'prerelease': r.prerelease,
                # Тег вида 4.9.0-v112 подходит игре версии 4.9.0
                'fits_game': bool(game_ver) and r.tag.startswith(game_ver + '-'),
            }
            for r in russian_releases()
        ]
        v = english_version()
        result['english_version'] = {'sha': v.short, 'date': v.date, 'message': ''}
    except GitHubError as e:
        result['error'] = str(e)
        log.warning('GitHub недоступен: %s', e)
    except Exception as e:
        result['error'] = f'Не удалось связаться с GitHub: {e}'
        log.warning('GitHub недоступен: %s', e, exc_info=True)

    return jsonify(result)


def _cache_meta() -> dict:
    if Config.CACHE_META.exists():
        try:
            return json.loads(Config.CACHE_META.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache_meta(**values) -> None:
    meta = _cache_meta()
    meta.update(values)
    Config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    Config.CACHE_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                 encoding='utf-8')


def _drop_old_cache(keep: Path) -> None:
    """
    Убирает переводы прошлых версий.

    Каждый релиз — отдельный файл на 15 МБ. Без уборки за год патчей у человека
    молча накопится больше гигабайта.
    """
    if not Config.CACHE_DIR.is_dir():
        return
    for old in Config.CACHE_DIR.glob('russian_*_global.ini'):
        if old == keep:
            continue
        try:
            old.unlink()
            log.info('Убран старый кэш: %s', old.name)
        except OSError as e:
            log.warning('Не удалось убрать %s: %s', old.name, e)


def ensure_fresh_sources(force: bool = False) -> dict:
    """
    Готовит свежие файлы перевода: качает, что изменилось, остальное берёт из кэша.

    Скачанное запоминаем вместе с версией (коммит для английского, тег для
    русского). Если на GitHub то же самое и файл на месте — не качаем заново:
    иначе каждая кнопка тянула бы 25 МБ и работала пять секунд вместо мгновения.

    Возвращает сведения о том, что использовано. Бросает GitHubError, если
    сети нет и в кэше пусто.
    """
    paths = load_paths()
    meta = _cache_meta()
    game_ver = _game_version_short()

    releases = russian_releases()
    chosen = pick_release(releases, game_ver)
    if chosen is None:
        raise GitHubError('На GitHub нет ни одного релиза перевода')

    en_dest = Config.CACHE_DIR / 'english_global.ini'
    ru_dest = Config.CACHE_DIR / f'russian_{chosen.tag}_global.ini'

    # --- английский ---
    en_ver = english_version()
    downloaded = False
    en_fresh = (not force and en_dest.is_file()
                and meta.get('english_tag') == en_ver.tag)
    if en_fresh:
        ui_log('Английский актуален, качать не нужно')
    else:
        downloaded = True
        ui_log('Качаю английский (StarStrings)...')
        size = download_english(en_dest, en_ver.path)
        _save_cache_meta(english_tag=en_ver.tag)
        ui_log(f'Английский готов: {size / 1048576:.1f} МБ')

    # --- русский ---
    ru_fresh = not force and ru_dest.is_file() and meta.get('ru_tag') == chosen.tag
    if ru_fresh:
        ui_log(f'Перевод {chosen.tag} уже скачан, качать не нужно')
    else:
        downloaded = True
        ui_log(f'Качаю перевод {chosen.tag} от {chosen.date}...')
        size = download_russian(chosen.tag, ru_dest)
        _save_cache_meta(ru_tag=chosen.tag)
        ui_log(f'Перевод готов: {size / 1048576:.1f} МБ, контрольная сумма сошлась')

    _drop_old_cache(keep=ru_dest)

    if paths['english'] != str(en_dest) or paths['russian'] != str(ru_dest):
        _store['en_path'] = _store['ru_path'] = None
    save_paths(english=str(en_dest), russian=str(ru_dest),
               source=f'github:{chosen.tag}', ru_tag=chosen.tag)

    fits = bool(game_ver) and chosen.tag.startswith(game_ver + '-')
    if game_ver and not fits:
        ui_log(f'Под твою игру ({game_ver}) перевода пока нет, '
               f'беру ближайший: {chosen.tag}', 'warn')

    return {'tag': chosen.tag, 'date': chosen.date, 'fits_game': fits,
            'game_version': game_ver, 'english_sha': en_ver.short,
            'english_date': en_ver.date, 'downloaded': downloaded}


def auto_prepare() -> dict | None:
    """
    Перед сборкой подтягивает свежие файлы, если работаем от GitHub.

    Нет сети — не беда, если в кэше что-то лежит: соберём из него и скажем
    об этом. Ручной режим не трогаем, там файлы выбрал человек.
    """
    paths = load_paths()
    if paths['source'] == 'manual':
        return None

    try:
        return ensure_fresh_sources()
    except GitHubError as e:
        if Path(paths['english']).is_file() and Path(paths['russian']).is_file():
            ui_log(f'GitHub недоступен ({e}). Собираю из ранее скачанного', 'warn')
            return None
        raise


# Состояние стартовой проверки. Страница спрашивает его у /api/status и
# показывает плашку: идёт проверка / всё свежее / что-то не так.
#
# Раньше файлы качались только по нажатию кнопки, и до первой сборки человек
# не знал, свежий ли у него перевод. Теперь программа выясняет это сама.
_startup: dict = {
    'state': 'checking',
    'message': 'Проверяю, есть ли свежий перевод…',
    'tag': '', 'date': '', 'fits_game': True, 'downloaded': False,
}


def _startup_message(info: dict) -> tuple[str, str]:
    """Текст плашки и её уровень (ok / warn) по результату проверки."""
    tag, date = info['tag'], info['date'] or 'неизвестной даты'
    if info['game_version'] and not info['fits_game']:
        return ('warn', f'Перевод {tag} собран не под твою игру '
                        f'({info["game_version"]}). Свежее пока нет.')
    got = 'скачан' if info['downloaded'] else 'уже был скачан'
    return ('ok', f'Перевод {tag} от {date} — самая свежая версия, {got}. '
                  f'Английский StarStrings актуален.')


def startup_check() -> None:
    """
    Стартовая проверка обновлений: выбрать самую свежую версию и скачать её.

    Идёт в фоновом потоке — страница обязана открыться сразу, а не ждать
    четверть минуты, пока приедут 25 МБ.
    """
    paths = load_paths()
    if paths['source'] == 'manual':
        _startup.update(state='manual',
                        message='Файлы указаны вручную — обновления не проверяются')
        ui_log('Файлы указаны вручную, обновления с GitHub не проверяю', 'warn')
        return

    ui_log('Проверяю, есть ли свежий перевод на GitHub...')
    try:
        _autodetect_game()
        info = ensure_fresh_sources()
    except GitHubError as e:
        current = load_paths()
        has_cache = (Path(current['english']).is_file()
                     and Path(current['russian']).is_file())
        if has_cache:
            # Причину показываем прямо в плашке, а не только в логе. «GitHub
            # недоступен» читается как «нет интернета», хотя чаще всего это
            # исчерпанный лимит запросов — и тогда человеку надо всего лишь
            # подождать, а не чинить сеть.
            _startup.update(state='offline', tag=current['ru_tag'],
                            message=f'{e} Пока работаю с тем, что скачано '
                                    f'раньше ({current["ru_tag"] or "локальные файлы"}).')
            ui_log(f'{e} Беру ранее скачанное', 'warn')
        else:
            _startup.update(state='error', message=str(e))
            ui_log(str(e), 'error')
        return
    except Exception as e:
        log.error('Стартовая проверка сорвалась: %s', e, exc_info=True)
        _startup.update(state='error', message=f'Не удалось проверить обновления: {e}')
        ui_log(f'Не удалось проверить обновления: {e}', 'error')
        return

    level, message = _startup_message(info)
    _startup.update(state='ready' if level == 'ok' else 'stale',
                    tag=info['tag'], date=info['date'],
                    fits_game=info['fits_game'], downloaded=info['downloaded'],
                    message=message)
    ui_log(message, level)

    # Читаем файлы заранее: 90 тысяч ключей разбираются пару секунд, и лучше
    # потратить их сейчас, чем после нажатия «Установить».
    try:
        ensure_loaded()
    except (FileNotFoundError, OSError) as e:
        log.warning('Не удалось заранее прочитать файлы: %s', e)


@app.route('/api/status')
def api_status():
    """Что показать в плашке о свежести перевода."""
    return jsonify(_startup)


# Обновление самой программы. Держим отдельно от свежести перевода: это разные
# вещи, и путать их в одной плашке — верный способ, чтобы человек не понял ни ту,
# ни другую.
#
# state: off — из исходников или репозиторий не задан; checking; none — у нас
# новейшая; available — вышла новее; applying — качаем и перезапускаемся; error.
# Стартовое состояние — 'checking', иначе страница успевает опросить нас раньше,
# чем проверка началась, увидит 'off' и перестанет спрашивать навсегда.
_update: dict = {'state': 'checking' if updates_supported() else 'off',
                 'current': APP_VERSION, 'version': '',
                 'notes': '', 'date': '', 'message': ''}

# Найденный релиз держим отдельно от _update: наружу он отдаётся как JSON,
# а для скачивания нужен сам объект со ссылкой и размером.
_update_found: dict = {'release': None}


def check_app_update() -> None:
    """Смотрит, не вышла ли новая версия программы. Ничего не качает."""
    if not updates_supported():
        _update.update(state='off', message='')
        return

    _update.update(state='checking', message='Проверяю версию программы…')
    try:
        release = latest_release()
    except GitHubError as e:
        # Не смогли спросить — не повод пугать красной плашкой: программа
        # работает, а состояние 'error' зовёт кнопку «Ещё раз», которой
        # обновляться не на что. Пишем в лог и молчим.
        _update.update(state='none', message='')
        log.warning('Проверка версии программы не удалась: %s', e)
        ui_log(f'Не удалось проверить версию программы: {e}', 'warn')
        return

    if release is None or not is_newer(release.tag):
        _update.update(state='none', message='')
        log.info('Программа последней версии (%s)', APP_VERSION)
        return

    _update_found['release'] = release
    _update.update(state='available', version=release.version, notes=release.notes,
                   date=release.date,
                   message=f'Вышла версия {release.version}'
                           + (f' от {release.date}' if release.date else '')
                           + f'. У тебя {APP_VERSION}.')
    ui_log(f'Доступно обновление программы: {release.version} '
           f'(у тебя {APP_VERSION})', 'warn')


@app.route('/api/update')
def api_update_state():
    return jsonify(_update)


@app.route('/api/update', methods=['POST'])
def api_update_apply():
    """
    Качает новую версию и перезапускает программу.

    Отвечаем сразу, а качаем и выходим в фоне: браузер должен успеть получить
    ответ и показать «обновляюсь», иначе человек увидит оборванную страницу
    и решит, что всё сломалось.
    """
    # 'error' сюда попадает только от сорвавшейся установки: там релиз уже
    # найден, и повтор осмыслен. Неудачная проверка версии до 'error'
    # не доводит — см. check_app_update.
    release: AppRelease | None = _update_found.get('release')
    if _update.get('state') not in ('available', 'error') or release is None:
        return jsonify({'error': 'Обновляться пока не на что'}), 400

    def run() -> None:
        try:
            ui_log(f'Качаю версию {release.version}...')
            staged = stage_update(release)
            ui_log('Скачано. Подменяю файлы и перезапускаюсь', 'ok')
            apply_update(staged)
        except (UpdateError, GitHubError, OSError) as e:
            _update.update(state='error', message=f'Обновление не удалось: {e}')
            ui_log(f'Обновление не удалось: {e}', 'error')
            log.error('Обновление не удалось: %s', e, exc_info=True)
            return
        # Пока exe жив, Windows не даст перезаписать ни его, ни библиотеки
        # рядом — поэтому bat ждёт нашего выхода, а мы выходим.
        time.sleep(1)
        os._exit(0)

    _update.update(state='applying',
                   message=f'Качаю версию {release.version}, программа перезапустится сама…')
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True, 'version': release.version})


@app.route('/api/fetch', methods=['POST'])
def api_fetch():
    """Качает английский и русский с GitHub и делает их текущими файлами."""
    tag = (request.get_json(silent=True) or {}).get('tag', '').strip()
    if not tag:
        return jsonify({'error': 'Не выбрана версия перевода'}), 400

    en_dest = Config.CACHE_DIR / 'english_global.ini'
    ru_dest = Config.CACHE_DIR / f'russian_{tag}_global.ini'

    try:
        ui_log(f'Качаю английский (StarStrings, {EN_BRANCH})...')
        en_v = english_version()
        en_size = download_english(en_dest, en_v.path)
        _save_cache_meta(english_tag=en_v.tag)
        ui_log(f'Английский готов: {en_size / 1024 / 1024:.1f} МБ')

        ui_log(f'Качаю русский ({tag})...')
        ru_size = download_russian(tag, ru_dest)
        _save_cache_meta(ru_tag=tag)
        ui_log(f'Русский готов: {ru_size / 1024 / 1024:.1f} МБ, контрольная сумма сошлась')
    except GitHubError as e:
        ui_log(str(e), 'error')
        return jsonify({'error': str(e)}), 400

    save_paths(english=str(en_dest), russian=str(ru_dest),
               source=f'github:{tag}', ru_tag=tag)

    # Файлы сменились — заставляем перечитать при следующем обращении.
    _store['en_path'] = _store['ru_path'] = None

    # Английский всегда свежий (в StarStrings только master), а русский релиз
    # привязан к патчу. Проверять их совпадение процентами бесполезно: соседние
    # патчи пересекаются на 98-99%, и порог не срабатывает, хотя без перевода
    # остаётся больше тысячи строк. Поэтому сверяем версию игры с тегом релиза
    # и показываем точное число непереведённых ключей.
    ensure_loaded()
    en_keys, ru_keys = set(_store['en']), set(_store['ru'])
    missing = len(en_keys - ru_keys)

    game_ver = _game_version_short()
    fits = bool(game_ver) and tag.startswith(game_ver + '-')

    if game_ver and not fits:
        ui_log(f'ВНИМАНИЕ: перевод {tag} собран не под твою версию игры ({game_ver}). '
               f'Без перевода останется {missing} строк', 'warn')
    elif missing:
        ui_log(f'В английском {missing} строк без перевода — останутся английскими', 'warn')
    else:
        ui_log('Перевод покрывает английский полностью')

    _startup.update(
        state='ready' if not (game_ver and not fits) else 'stale',
        tag=tag, fits_game=fits, downloaded=True,
        message=(f'Перевод {tag} собран не под твою игру ({game_ver}).'
                 if game_ver and not fits
                 else f'Перевод {tag} скачан и выбран вручную.'))

    return jsonify({
        'ok': True,
        'tag': tag,
        'english_size': en_size,
        'russian_size': ru_size,
        'game_version': game_ver,
        'fits_game': fits,
        'missing': missing,
        'total': len(en_keys),
    })


@app.route('/api/game', methods=['GET', 'POST'])
def api_game():
    """Папка игры и ветка. Веток может быть несколько — LIVE, PTU, HOTFIX."""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        game_dir = (data.get('game_dir') or '').strip()
        branch = (data.get('branch') or '').strip()
        save_paths(game_dir=game_dir, branch=branch)

    game_dir, branches, branch = _autodetect_game()

    return jsonify({
        'game_dir': game_dir,
        'game_dir_ok': bool(game_dir) and Path(game_dir).is_dir(),
        'branches': branches,
        'branch': branch,
    })


@app.route('/api/install', methods=['POST'])
def api_install():
    """
    Ставит в игру то, что выбрано в «Что переводим»:
    английский с блюпринтами — если включён этот режим, иначе русский перевод.
    """
    paths = load_paths()
    game_dir, branch = paths['game_dir'], paths['branch']
    if not game_dir or not branch:
        return jsonify({'error': 'Не выбрана папка игры или ветка'}), 400

    if load_profile().get(ENGLISH_ID):
        return _install_english_mode(Path(game_dir) / branch)

    # Собираем прямо здесь: заставлять человека жать две кнопки подряд, чтобы
    # получить очевидный результат, — лишний шаг на ровном месте.
    try:
        build = _do_build()
    except GitHubError as e:
        ui_log(str(e), 'error')
        return jsonify({'error': str(e)}), 400

    source = Config.OUTPUT_DIR / 'global.ini'
    if not source.is_file():
        return jsonify({'error': 'Сборка не создала файл'}), 400

    ui_log(f'Устанавливаю в {branch}...')
    result = install(Path(game_dir) / branch, source)

    for m in result.messages:
        ui_log(m, 'warn' if result.cfg_status in ('needs_manual', 'conflict') and m == result.cfg_message else 'info')
    if not result.ok:
        return jsonify({'error': '; '.join(result.messages)}), 400

    return jsonify({
        'ok': True, 'mode': 'russian',
        'installed_to': result.installed_to,
        'backup': result.backup,
        'cfg_status': result.cfg_status,
        'cfg_message': result.cfg_message,
        'messages': result.messages,
        'build': build,
    })


def _install_english_mode(branch_dir: Path):
    """Режим «английский с блюпринтами»: качаем английский и ставим без перевода."""
    en_dest = Config.CACHE_DIR / 'english_global.ini'
    try:
        en_v = english_version()
        if not (en_dest.is_file() and _cache_meta().get('english_tag') == en_v.tag):
            ui_log('Качаю английский с блюпринтами (StarStrings)...')
            download_english(en_dest, en_v.path)
            _save_cache_meta(english_tag=en_v.tag)
        else:
            ui_log('Английский актуален, качать не нужно')
    except GitHubError as e:
        if not en_dest.is_file():
            ui_log(str(e), 'error')
            return jsonify({'error': str(e)}), 400
        ui_log(f'GitHub недоступен ({e}), ставлю из кэша', 'warn')

    ui_log(f'Ставлю английский в {branch_dir.name}...')
    result = install_english(branch_dir, en_dest)
    for m in result.messages:
        ui_log(m)
    if not result.ok:
        return jsonify({'error': '; '.join(result.messages)}), 400
    return jsonify({
        'ok': True, 'mode': 'english',
        'installed_to': result.installed_to,
        'backup': result.backup,
        'cfg_message': result.cfg_message,
    })


@app.route('/api/categories')
def api_categories():
    # ?defaults=1 — вернуть рекомендованный набор, а не сохранённый выбор.
    # Нужно кнопке «По умолчанию», чтобы откатить галочки.
    if request.args.get('defaults'):
        profile = default_profile()
    else:
        profile = load_profile()
    return jsonify([
        {'id': c.id, 'title': c.title, 'hint': c.hint, 'enabled': profile.get(c.id, False)}
        for c in CATEGORIES
    ])


@app.route('/api/profile', methods=['GET', 'POST'])
def api_profile():
    if request.method == 'GET':
        return jsonify(load_profile())

    incoming = request.get_json(silent=True) or {}
    profile = default_profile()
    profile.update({k: bool(v) for k, v in incoming.items() if k in profile})
    save_profile(profile)
    if profile.get(ENGLISH_ID):
        ui_log('Режим: английский с блюпринтами (без перевода)')
    elif profile.get(FULL_ID):
        ui_log('Режим: полный русский (всё, что переведено)')
    else:
        ui_log(f'Режим: описания — {sum(1 for c in CATEGORIES if profile.get(c.id))} из {len(CATEGORIES)} категорий')
    return jsonify({'ok': True})


@app.route('/api/counts')
def api_counts():
    """
    Сколько ключей забирает каждая категория — для окна «Что переводим».

    Тоже подтягивает файлы: без них считать нечего, а окно должно открываться
    и на свежей установке, а не падать с «файл не найден».
    """
    try:
        auto_prepare()
    except GitHubError as e:
        return jsonify({'error': str(e)}), 400
    ensure_loaded()
    profile = load_profile()
    by_cat: dict[str, int] = {}
    translated = 0
    for key, en_value in _store['en'].items():
        d = classify(key, profile, en_value)
        if not d.translate or key not in _store['ru']:
            continue
        translated += 1
        by_cat[d.category] = by_cat.get(d.category, 0) + 1
    return jsonify({
        'by_category': by_cat,
        'translated': translated,
        'total': len(_store['en']),
        'percent': round(translated / len(_store['en']) * 100, 1) if _store['en'] else 0,
    })


def _do_build(skip_invalid: bool = True) -> dict:
    """
    Общая часть кнопок «Собрать» и «Установить».

    Обе сначала подтягивают свежие файлы: человек нажимает кнопку, чтобы
    получить актуальный перевод, а не чтобы думать про версии и загрузки.
    """
    source_info = auto_prepare()
    ensure_loaded()
    profile = load_profile()

    overrides = load_overrides(Config.OVERRIDES_FILE)
    if overrides:
        ui_log(f'Ручных исправлений загружено: {len(overrides)}')

    ui_log('Собираю локализацию...')
    out_path = Config.OUTPUT_DIR / 'global.ini'
    stats = merge(_store['en_path'], _store['ru'], out_path, profile, skip_invalid, overrides)

    percent = round(stats.translated / stats.total_en * 100, 1) if stats.total_en else 0
    ui_log(f'Готово: переведено {stats.translated:,} из {stats.total_en:,} ключей ({percent}%)'
           .replace(',', ' '), 'ok')
    if stats.skipped_invalid:
        ui_log(f'Пропущено битых переводов: {stats.skipped_invalid}', 'warn')
    if stats.skipped_no_russian:
        ui_log(f'Нет русского перевода: {stats.skipped_no_russian}', 'warn')
    ui_log(f'Файл: {out_path}')

    return {
        'ok': True,
        'path': str(out_path),
        'translated': stats.translated,
        'total': stats.total_en,
        'percent': percent,
        'skipped_invalid': stats.skipped_invalid,
        'overridden': stats.overridden,
        'source': source_info,
    }


@app.route('/api/build', methods=['POST'])
def api_build():
    skip_invalid = bool((request.get_json(silent=True) or {}).get('skip_invalid', True))
    try:
        return jsonify(_do_build(skip_invalid))
    except GitHubError as e:
        ui_log(str(e), 'error')
        return jsonify({'error': str(e)}), 400


@app.route('/api/log')
def api_log():
    return jsonify(list(_ui_log))


# Отметка последней активности страницы. Пока вкладка открыта, она шлёт пинги;
# пропали пинги — значит её закрыли, и держать сервер незачем.
_last_ping = {'at': time.monotonic()}
_HEARTBEAT_TIMEOUT = 8      # молчит дольше — считаем, что вкладку закрыли
_HEARTBEAT_GRACE = 20       # но первые секунды после старта не трогаем


@app.route('/api/ping', methods=['POST'])
def api_ping():
    _last_ping['at'] = time.monotonic()
    return '', 204


@app.route('/api/closing', methods=['POST'])
def api_closing():
    """
    Вкладку закрывают. Отматываем время активности назад, чтобы сторож закрыл
    сервер быстро. Но не выходим сразу: перезагрузка страницы шлёт тот же
    сигнал, а следом — новый пинг, который всё отменит.
    """
    _last_ping['at'] = time.monotonic() - _HEARTBEAT_TIMEOUT + 2
    return '', 204


def _heartbeat_watchdog() -> None:
    """Закрывает программу, когда страница перестала подавать признаки жизни."""
    time.sleep(_HEARTBEAT_GRACE)
    while True:
        time.sleep(1)
        if time.monotonic() - _last_ping['at'] > _HEARTBEAT_TIMEOUT:
            log.info('Страница закрыта, останавливаю программу')
            os._exit(0)


@app.route('/api/quit', methods=['POST'])
def api_quit():
    """
    Останавливает программу.

    Без консоли закрыть сервер иначе нечем: чёрного окна с Ctrl+C больше нет,
    а процесс pythonw не виден в панели задач. Выходим отложенно, чтобы успеть
    ответить браузеру.
    """
    def shutdown():
        time.sleep(0.5)
        log.info('Выход по кнопке в интерфейсе')
        # Штатного способа остановить сервер разработки в свежем Werkzeug нет,
        # а нам и нечего доделывать — merge пишет файл целиком и уже завершён.
        os._exit(0)

    threading.Thread(target=shutdown, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/download')
def api_download():
    return send_from_directory(Config.OUTPUT_DIR, 'global.ini', as_attachment=True)


@app.errorhandler(FileNotFoundError)
def handle_missing(e: FileNotFoundError):
    log.error('Файл локализации не найден', exc_info=True)
    ui_log(str(e), 'error')
    return jsonify({'error': str(e)}), 400


@app.errorhandler(Exception)
def handle_any(e: Exception):
    """
    Любая необработанная ошибка попадает в лог и в интерфейс.

    Верхний уровень веб-обработчика — то место, где ловить Exception уместно:
    иначе Flask отдаёт голый HTML «500 Internal Server Error», причина уходит
    только в stderr, и снаружи это выглядит как «кнопка просто не работает».
    """
    # Обычные HTTP-ответы вроде 404 — не аварии. Браузер сам просит
    # /favicon.ico, и без этой проверки такой запрос превращался в 500
    # и красную строку в логе на ровном месте.
    if isinstance(e, HTTPException):
        return e

    log.error('Необработанная ошибка: %s', e, exc_info=True)
    ui_log(f'{type(e).__name__}: {e}', 'error')
    return jsonify({'error': f'{type(e).__name__}: {e}'}), 500


def _port_is_busy() -> bool:
    """Занят ли наш порт — значит программа уже запущена."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((Config.HOST, Config.PORT)) == 0


def main() -> None:
    # Режим окна выбора файла: программа вызывает саму себя с этим флагом.
    # Проверка обязана быть первой — до всякого поднятия сервера.
    if len(sys.argv) > 1 and sys.argv[1] == FILE_DIALOG_FLAG:
        run_dialog_to_file(
            sys.argv[2] if len(sys.argv) > 2 else 'Выберите файл',
            sys.argv[3] if len(sys.argv) > 3 else '',
            sys.argv[4] if len(sys.argv) > 4 else 'dialog_result.txt',
        )
        return

    url = f'http://{Config.HOST}:{Config.PORT}'

    # Второй запуск не поднимет сервер (порт занят) и без этой проверки просто
    # молча умрёт. Показываем уже открытую программу.
    if _port_is_busy():
        log.info('Программа уже запущена, открываю %s', url)
        webbrowser.open(url)
        return

    just_updated = UPDATED_FLAG in sys.argv
    if just_updated:
        log.info('Запуск после обновления, вкладку не открываю')

    def open_browser():
        time.sleep(1.5)
        webbrowser.open(url)

    if not just_updated:
        threading.Thread(target=open_browser, daemon=True).start()
    # Сторож: закроет программу, когда закроют вкладку в браузере. Иначе
    # сервер висит в фоне и держит свои файлы — как раз то, что мешало пересборке.
    threading.Thread(target=_heartbeat_watchdog, daemon=True).start()

    ui_log('Программа запущена')
    log.info('Слушаю %s', url)

    # Обновления проверяем и качаем сразу, не дожидаясь нажатия кнопки:
    # человек должен видеть, что у него свежий перевод, ещё до сборки.
    def startup_all() -> None:
        # По очереди, а не двумя нитями: GitHub без токена даёт 60 запросов
        # в час, и параллелить их незачем.
        startup_check()
        check_app_update()

    threading.Thread(target=startup_all, daemon=True).start()

    # Встроенный сервер Flask — для разработки: он сам про это предупреждает
    # и держит нагрузку хуже. В раздаваемой сборке поднимаем waitress.
    if Config.DEBUG:
        app.run(host=Config.HOST, port=Config.PORT, debug=True)
        return

    try:
        from waitress import serve
    except ImportError:
        log.warning('waitress не установлен, поднимаю встроенный сервер Flask')
        app.run(host=Config.HOST, port=Config.PORT)
        return

    serve(app, host=Config.HOST, port=Config.PORT, threads=8)


if __name__ == '__main__':
    main()
