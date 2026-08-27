"""
Нативное окно выбора файла.

Запускается отдельным процессом: обработчики запросов Flask живут в рабочих
потоках, а tkinter требует главный поток и из потока подвешивает сервер.

Результат отдаётся через файл, а не через stdout: в собранном оконном exe
у процесса нет консоли, sys.stdout равен None, и печатать туда нечего.

    python file_dialog.py "Заголовок" "C:\\стартовая\\папка" "C:\\куда\\записать.txt"
"""
import sys
from pathlib import Path

# Импорт на уровне модуля, а не внутри функции: так PyInstaller гарантированно
# видит зависимость и кладёт tkinter в сборку.
import tkinter as tk
from tkinter import filedialog


def ask_path(title: str, initial_dir: str = '') -> str:
    root = tk.Tk()
    root.withdraw()
    # Поверх браузера — иначе окно открывается позади и выглядит как зависание.
    root.attributes('-topmost', True)
    path = filedialog.askopenfilename(
        parent=root,
        title=title,
        initialdir=initial_dir or None,
        filetypes=[('Файлы локализации', '*.ini'), ('Все файлы', '*.*')],
    )
    root.destroy()
    return path or ''


def ask_dir(title: str, initial_dir: str = '') -> str:
    """То же окно, но для выбора папки — нужно для папки игры."""
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    path = filedialog.askdirectory(parent=root, title=title, initialdir=initial_dir or None)
    root.destroy()
    return path or ''


def run_dialog_to_file(title: str, initial_dir: str, out_file: str,
                       mode: str = 'file') -> None:
    """Показывает окно и кладёт выбранный путь в файл (пустой файл = отмена)."""
    path = ask_dir(title, initial_dir) if mode == 'dir' else ask_path(title, initial_dir)
    Path(out_file).write_text(path, encoding='utf-8')


if __name__ == '__main__':
    run_dialog_to_file(
        sys.argv[1] if len(sys.argv) > 1 else 'Выберите файл',
        sys.argv[2] if len(sys.argv) > 2 else '',
        sys.argv[3] if len(sys.argv) > 3 else 'dialog_result.txt',
        sys.argv[4] if len(sys.argv) > 4 else 'file',
    )
