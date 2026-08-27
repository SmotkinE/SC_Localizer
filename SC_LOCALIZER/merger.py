"""
Слияние русского перевода в английский global.ini.

Логика построчной замены взята из рабочего merge_translations.py:
идём по английскому файлу, подменяем значение только у отобранных ключей,
всё остальное отдаём байт-в-байт как было.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from ini_io import LINE_RX
from logger import get_logger
from rules import classify
from validator import Issue, check_pair

log = get_logger(__name__)

# Технический хвост, который MrKraken (StarStrings) дописывает к описаниям
# контрактов: награда репутацией, пулы чертежей, очки сценария. Русский перевод
# его не содержит, поэтому берём хвост из английского и клеим к переводу.
#
# Начало хвоста — первый из этих <EM4>-блоков. Раньше ловился только
# 'Potential Blueprints', из-за чего терялись и 'Reputation Awarded' (он идёт
# раньше), и варианты 'Multiple Blueprint Pools' / 'Scenario Progress' — а с
# ними у игрока пропадали блюпринты, которые видит английский игрок.
_TAIL_RX = re.compile(
    r'(?:\\n)*<EM4>\s*(?:'
    r'Reputation Awarded'
    r'|Potential Blueprints'
    r'|Multiple Blueprint Pools'
    r'|Scenario Progress'
    r')'
)

# Та же приписка, но в НАЗВАНИИ контракта — короткая метка в самом конце строки:
#   'Salvager Needed (...) <EM4>[150 Rep] [BP]*</EM4>'
#   'Ship In Distress <EM4>[300 Rep]</EM4>'
#   'Mission Name <EM4>[BP]</EM4>'
# По ней игрок видит прямо в списке контрактов, что за миссию дают чертёж.
# В режиме «Полный русский» названия берутся из перевода, а там метки нет —
# и 673 миссии теряли пометку о блюпринтах. Забираем хвост из английского,
# ровно как в описаниях.
#
# Регулярка нарочно узкая: число+Rep или BP, и только в самом конце значения.
# В описаниях встречаются другие <EM4>[...]</EM4> — например
# '[Regional Variants] example locations: ...' — их трогать нельзя.
_TITLE_TAIL_RX = re.compile(r'\s*<EM4>\s*\[(?:[-\d/]+\s*Rep|BP)[^<]*</EM4>\s*$')


def _split_tail(value: str) -> tuple[str, str]:
    """Делит значение на (основной текст, технический хвост StarStrings)."""
    m = _TAIL_RX.search(value) or _TITLE_TAIL_RX.search(value)
    if m:
        return value[:m.start()], value[m.start():]
    return value, ''


def _detect_eol(line: str) -> str:
    for eol in ('\r\n', '\n', '\r'):
        if line.endswith(eol):
            return eol
    return '\n'


@dataclass
class MergeStats:
    total_en: int = 0
    translated: int = 0
    skipped_no_russian: int = 0
    skipped_by_rules: int = 0
    skipped_invalid: int = 0
    overridden: int = 0
    tails_kept: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)


def merge(en_path: Path, ru_data: dict[str, str], out_path: Path,
          profile: dict[str, bool], skip_invalid: bool = True,
          overrides: dict[str, str] | None = None) -> MergeStats:
    """
    Собирает итоговый файл.
    skip_invalid — не подставлять перевод, если он ломает плейсхолдеры (лучше
    оставить английский текст, чем испорченный русский).
    overrides — ручные исправления, применяются поверх категорий.
    """
    stats = MergeStats()
    overrides = overrides or {}
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with en_path.open('r', encoding='utf-8', errors='replace') as fin, \
         out_path.open('w', encoding='utf-8', newline='') as fout:
        for line in fin:
            eol = _detect_eol(line)
            core = line[:-len(eol)] if line.endswith(eol) else line
            m = LINE_RX.match(core)
            if not m:
                fout.write(line)
                continue

            key = m.group(2).strip()
            stats.total_en += 1
            en_value = m.group(4)

            # Ручное исправление побеждает и правила, и общий перевод:
            # если строку вписали руками, значит именно её и хотят видеть.
            category = None
            if key in overrides:
                ru_value = overrides[key]
                stats.overridden += 1
            else:
                decision = classify(key, profile, en_value)
                if not decision.translate:
                    stats.skipped_by_rules += 1
                    fout.write(line)
                    continue

                if key not in ru_data:
                    stats.skipped_no_russian += 1
                    fout.write(line)
                    continue

                ru_value = ru_data[key]
                category = decision.category

            issues = check_pair(key, en_value, ru_value)
            stats.issues.extend(issues)
            if skip_invalid and any(i.severity == 'error' for i in issues):
                stats.skipped_invalid += 1
                fout.write(line)
                continue

            # Чертежи и метки [BP] — из оригинала, основной текст — из перевода.
            en_main, tail = _split_tail(en_value)
            ru_main, _ = _split_tail(ru_value)
            if tail and not en_main.strip():
                # Английское значение — одна голая метка, подставлять некуда.
                fout.write(line)
                stats.skipped_by_rules += 1
                continue
            # Хвост уже несёт свой отбивающий пробел; без rstrip перевод,
            # заканчивающийся пробелом, давал бы двойной.
            final_value = (ru_main.rstrip(' \t') if tail else ru_main) + tail
            if tail:
                stats.tails_kept += 1

            fout.write(f'{m.group(1)}{key}{m.group(3)}{final_value}{eol}')
            stats.translated += 1
            if category:
                stats.by_category[category] = stats.by_category.get(category, 0) + 1

    log.info('Собран %s: переведено %d из %d ключей (%.1f%%), пропущено по правилам %d, '
             'из-за ошибок %d, нет перевода %d, ручных исправлений %d, '
             'сохранено меток StarStrings %d',
             out_path.name, stats.translated, stats.total_en,
             stats.translated / stats.total_en * 100 if stats.total_en else 0,
             stats.skipped_by_rules, stats.skipped_invalid, stats.skipped_no_russian,
             stats.overridden, stats.tails_kept)
    return stats
