"""
Проверки качества перевода.

Смысл: сломанный плейсхолдер или тег в игре выглядит как мусор на экране,
а найти его потом в файле на 90 тысяч строк невозможно. Ловим до сборки.
"""
import re
from collections import Counter
from dataclasses import dataclass

# Форматные спецификаторы движка: %ls, %d, %u, %i, %S, %1$s
FORMAT_RX = re.compile(r'%(?:\d+\$)?(?:ls|lu|s|S|d|u|i|f)')
# Разметка подсветки в описаниях
TAG_RX = re.compile(r'</?EM\d>|</?B>|</?I>|<span[^>]*>|</span>')
# Подстановки движка: ~mission(Amount), ~action(player|interact)
TILDE_RX = re.compile(r'~[a-zA-Z_]+\(')


@dataclass
class Issue:
    key: str
    severity: str   # 'error' — сломает вывод, 'warn' — подозрительно
    message: str


def check_pair(key: str, en_value: str, ru_value: str) -> list[Issue]:
    """Сравнивает английское и русское значение одного ключа."""
    issues: list[Issue] = []

    if not ru_value.strip():
        # Пустой перевод опасен только если в оригинале был текст. Пара пустых
        # значений — это просто неиспользуемый ключ, тревожить из-за него незачем.
        if en_value.strip():
            issues.append(Issue(key, 'error', 'Пустой перевод — затрёт английский текст'))
        return issues

    if ru_value == en_value:
        issues.append(Issue(key, 'warn', 'Перевод совпадает с оригиналом'))

    for rx, name in ((FORMAT_RX, 'плейсхолдер'), (TILDE_RX, 'подстановка'), (TAG_RX, 'тег разметки')):
        en_counts, ru_counts = Counter(rx.findall(en_value)), Counter(rx.findall(ru_value))
        if en_counts == ru_counts:
            continue
        lost = en_counts - ru_counts
        added = ru_counts - en_counts
        parts = []
        if lost:
            parts.append('потерян: ' + ', '.join(f'{t}×{n}' for t, n in lost.items()))
        if added:
            parts.append('лишний: ' + ', '.join(f'{t}×{n}' for t, n in added.items()))
        severity = 'error' if (name != 'тег разметки' and lost) else 'warn'
        issues.append(Issue(key, severity, f'{name.capitalize()} — ' + '; '.join(parts)))

    return issues
