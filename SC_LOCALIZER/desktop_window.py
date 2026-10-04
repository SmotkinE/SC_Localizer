"""
Окно программы на Windows: та же страница, но в собственном окне, а не во
вкладке браузера.

Страницу рисует движок Edge (WebView2), встроенный в Windows 10/11. Python,
pywebview и всё нужное едут внутри сборки — пользователю ставить ничего не
надо. Если движка в системе нет, программа работает через браузер, как раньше.

Заодно окно снимает целый класс проблем вкладки: закрыл окно — программа
закрылась, без сторожа, пингов и угадывания, перезагрузка это или выход.
"""
import os
import sys

from logger import get_logger

log = get_logger(__name__)

TITLE = 'SC Localizer'

# Метка в строке браузера окна. По ней сервер отличает окно от вкладки:
# если программа работает в окне, а страницу открыли во вкладке (старая
# вкладка дождалась перезапуска после обновления), вкладке говорим, что её
# можно закрыть. Страница от этой строки не зависит.
USER_AGENT = 'SCLocalizerWindow'

# Ниже не ужимаем: при совсем маленьком окне кнопки начинают налезать.
MIN_HEIGHT = 520

# Открытое окно — чтобы второй запуск программы мог вытащить его вперёд.
_window = None

# Где окно стояло, пока подгонка не подняла его, чтобы оно влезло по высоте.
# Ужалось обратно — возвращаем туда. set_top — куда поставили мы сами: если
# окно оказалось в другом месте, его передвинул человек, и старое место забываем.
_position = {'home': None, 'set_top': None}


class _Api:
    """То, что страница может вызвать у окна через window.pywebview.api."""

    def fit(self, content: float, dpr: float) -> None:
        """
        Подгоняет высоту окна под содержимое страницы: открыли лог или окно
        выбора перевода — выросло, закрыли — ужалось. Ширину не трогаем.

        Считаем средствами Windows, в настоящих пикселях экрана. Сама страница
        знает о себе не всё: «верх окна» для неё — верх страницы, без заголовка,
        и на этом подгонка ошибалась ровно на высоту заголовка.
        """
        try:
            _fit_height(float(content) * float(dpr), float(dpr))
        except Exception as e:
            log.warning('Не удалось подогнать окно: %s', e)


def _fit_height(content_px: float, dpr: float) -> None:
    """Высота окна = содержимое + заголовок и рамка, но не выше рабочей области."""
    import ctypes
    from ctypes import wintypes

    if _window is None or _window.native is None:
        return

    class MONITORINFO(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                    ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]

    u32 = ctypes.windll.user32
    u32.MonitorFromWindow.restype = wintypes.HMONITOR
    u32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    u32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]

    hwnd = wintypes.HWND(_window.native.Handle.ToInt64())
    # Развёрнутое на весь экран или свёрнутое окно человек поставил сам — не трогаем.
    if u32.IsZoomed(hwnd) or u32.IsIconic(hwnd):
        return

    win, client = wintypes.RECT(), wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(win))
    u32.GetClientRect(hwnd, ctypes.byref(client))
    frame = (win.bottom - win.top) - (client.bottom - client.top)

    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    u32.GetMonitorInfoW(u32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))  # 2 — ближайший
    work = info.rcWork  # экран без панели задач

    height = round(content_px) + frame
    height = min(height, work.bottom - work.top)
    height = max(height, round(MIN_HEIGHT * dpr))

    top = win.top
    if _position['set_top'] is not None and win.top != _position['set_top']:
        _position['home'] = None  # окно передвинули руками — это теперь его место

    if top + height > work.bottom:
        # Внизу не хватает места — поднимаем окно, а не режем содержимое.
        if _position['home'] is None:
            _position['home'] = win.top
        top = max(work.top, work.bottom - height)
    elif _position['home'] is not None:
        # Ужалось — возвращаем на прежнее место, насколько оно теперь влезает.
        top = min(_position['home'], work.bottom - height)
        if top == _position['home']:
            _position['home'] = None
    _position['set_top'] = top

    if abs(height - (win.bottom - win.top)) <= 2 and top == win.top:
        return  # не дёргать окно из-за пары пикселей
    SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
    u32.SetWindowPos(hwnd, None, win.left, top, win.right - win.left, height,
                     SWP_NOZORDER | SWP_NOACTIVATE)


def available() -> bool:
    """
    Можно ли открыть окно.

    По умолчанию окно — только у собранной программы на Windows; из исходников
    привычнее браузер с инструментами разработчика. Переменная SC_UI это
    перебивает: browser — всегда браузер, window — окно и из исходников.
    """
    choice = os.getenv('SC_UI', '').strip().lower()
    if choice == 'browser' or sys.platform != 'win32':
        return False
    if choice != 'window' and not getattr(sys, 'frozen', False):
        return False
    try:
        # Внутренняя функция pywebview, поэтому версия закреплена в
        # requirements. Поменяется — сработает except, и будет браузер.
        from webview.platforms.winforms import _is_chromium
        return bool(_is_chromium())
    except Exception as e:
        log.warning('Окно недоступно, открою в браузере: %s', e)
        return False


def run(url: str) -> bool:
    """Открывает окно и ждёт, пока его закроют. False — открыть не вышло."""
    global _window
    try:
        import webview

        # Ссылка «Скачать global.ini» — без этого движок её молча глотает.
        webview.settings['ALLOW_DOWNLOADS'] = True
        # Высота стартовая: страница сразу подгонит её под содержимое.
        _window = webview.create_window(TITLE, url, width=960, height=720,
                                        min_size=(640, MIN_HEIGHT), js_api=_Api())
        # Только Edge. Без явного указания pywebview на старой системе может
        # откатиться на движок Internet Explorer, а страница на нём не работает.
        webview.start(gui='edgechromium', user_agent=USER_AGENT)
    except Exception as e:
        log.warning('Окно не открылось: %s', e, exc_info=True)
        # Дальше программа работает в браузере — и он должен получать сам
        # интерфейс, а не надпись «программа открыта в своём окне».
        _window = None
        return False
    return True


def is_open() -> bool:
    """Работает ли программа в своём окне."""
    return _window is not None


def focus() -> bool:
    """Выводит окно вперёд. False — окна нет, программа работает в браузере."""
    if _window is None:
        return False
    try:
        _window.restore()
        # Простого «на передний план» у pywebview нет, а Windows не даёт
        # чужому процессу отбирать фокус. Поверх всех на миг — и обратно.
        _window.on_top = True
        _window.on_top = False
    except Exception as e:
        log.warning('Не удалось вывести окно вперёд: %s', e)
    return True
