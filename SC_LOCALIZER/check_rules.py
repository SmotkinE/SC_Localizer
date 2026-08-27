"""
Проверка правил на реальных файлах: сколько ключей ловит каждая категория
и что именно в неё попало. Запускать после правки rules.py.

    python check_rules.py            # сводка
    python check_rules.py mission_obj  # + примеры по категории
"""
import io
import random
import sys
from collections import Counter

from config import Config
from ini_io import load_ini
from rules import CATEGORIES, classify, default_profile
from validator import check_pair

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


def main() -> None:
    en = load_ini(Config.ENGLISH_INI)
    ru = load_ini(Config.RUSSIAN_INI)
    profile = default_profile()

    hits: dict[str, list[str]] = {c.id: [] for c in CATEGORIES}
    translated, no_ru = 0, 0
    reasons: Counter = Counter()

    for key, en_value in en.items():
        d = classify(key, profile, en_value)
        if not d.translate:
            reasons[d.reason] += 1
            continue
        hits[d.category].append(key)
        if key in ru:
            translated += 1
        else:
            no_ru += 1

    print(f'\nВсего ключей EN: {len(en)}')
    print(f'Отобрано к переводу: {translated + no_ru} ({(translated + no_ru) / len(en) * 100:.1f}%)')
    print(f'  из них есть перевод: {translated}')
    print(f'  нет русского: {no_ru}')

    print('\n--- по категориям ---')
    for c in CATEGORIES:
        mark = ' ' if c.enabled_by_default else '.'
        print(f' [{mark}] {len(hits[c.id]):6d}  {c.title}')

    print('\n--- топ причин отказа ---')
    for reason, n in reasons.most_common(8):
        print(f'  {n:6d}  {reason}')

    # Качество того, что реально подставится
    errors = warns = 0
    for c in CATEGORIES:
        for k in hits[c.id]:
            if k not in ru:
                continue
            for i in check_pair(k, en[k], ru[k]):
                if i.severity == 'error':
                    errors += 1
                else:
                    warns += 1
    print(f'\n--- качество отобранного ---')
    print(f'  ошибок (перевод будет пропущен): {errors}')
    print(f'  предупреждений: {warns}')

    if len(sys.argv) > 1:
        cat = sys.argv[1]
        random.seed(11)
        keys = hits.get(cat, [])
        print(f'\n--- примеры {cat} ({len(keys)}) ---')
        for k in random.sample(keys, min(15, len(keys))):
            print(f'  {k}\n     EN: {en[k][:95]}\n     RU: {ru.get(k, "<нет>")[:95]}')


if __name__ == '__main__':
    main()
