"""
Правила отбора ключей: что переводим, а что оставляем английским.

Главный принцип — переводим ОПИСАНИЯ и текст, а не НАЗВАНИЯ.
Ключ разбирается на токены (`item_Descarma_barrel_comp_s2` -> item, Descarma, barrel, ...),
дальше решение принимается по токенам, а не по подстроке в сыром ключе.
Так `vehicle_DescRSI_Meteor` (описание корабля) не путается с `vehicle_NameRSI_Meteor`.

Порядок решения:
  1. Жёсткие исключения (технические ключи) — всегда English.
  2. Токены-названия (Name*, *Title, Heading) — всегда English, что бы ни совпало дальше.
  3. Категории — включаемые/отключаемые группы.
Первое сработавшее исключение побеждает любую категорию.
"""
import re
from dataclasses import dataclass
from typing import Callable, Optional

# ---------- разбор ключа на токены ----------

_SUFFIX_RX = re.compile(r',\s*[A-Za-z]$')  # хвост ",P" — вариант строки, к смыслу не относится


def tokenize(key: str) -> list[str]:
    """`Hockrow_FacilityDelve_P3M1_obj_short_03,P` -> [hockrow, facilitydelve, p3m1, obj, short, 03]"""
    base = _SUFFIX_RX.sub('', key.strip())
    return [t for t in re.split(r'[_\-]', base) if t]


def _tokens_lower(key: str) -> list[str]:
    return [t.lower() for t in tokenize(key)]


# ---------- 1. жёсткие исключения ----------

# Английские заглушки-плейсхолдеры: '[PH] Outcasts Focus' — переводить нечего.
_PLACEHOLDER_VALUE_RX = re.compile(r'^\s*(\[PH\]|\[WIP\]|<PH>|PH:)', re.I)

_HARD_EXCLUDE_SUBSTR = (
    'appname_journal',   # техническое имя приложения, не текст
)


def is_hard_excluded(key: str) -> bool:
    kl = key.lower()
    return any(s in kl for s in _HARD_EXCLUDE_SUBSTR)


# ---------- 2. токены-названия ----------

# Токен считается "названием", если начинается с name (Name, NameKRON, NameGRIN)
# либо является заголовком (Title, ShortTitle, JournalTitle, Heading, SubHeading).
_NAME_TOKEN_RX = re.compile(
    r'^(name.*|.*title|heading|subheading|subhead|label|caption)$', re.I
)

# Слова, где 'desc' встречается не в значении "описание" — чтобы не ловить их случайно.
_FALSE_DESC_RX = re.compile(r'^(descend\w*|descent\w*|descriptor)$', re.I)


def has_name_token(key: str) -> bool:
    return any(_NAME_TOKEN_RX.match(t) for t in tokenize(key))


# ---------- строительные блоки категорий ----------

def _has_desc_token(key: str) -> bool:
    """Любой токен, содержащий 'desc': Desc, DescRSI, Descarma, Description, LongDesc, modedesc."""
    for t in tokenize(key):
        if _FALSE_DESC_RX.match(t):
            continue
        if 'desc' in t.lower():
            return True
    return False


_OBJ_TOKEN_RX = re.compile(r'^(sub|main|mission)?obj(ective)?s?\d*$', re.I)


def _has_obj_token(key: str) -> bool:
    """Цели миссии: obj, Obj, objective, Objective01, subobj, MainObjective, MissionObj."""
    return any(_OBJ_TOKEN_RX.match(t) for t in tokenize(key))


# Часть целей не имеет токена obj вообще: BoardShip_goto_long, Kaboos_CollectData_short,
# SOO2_Intro_Mission_1-0_Long. Опознаём их по хвосту _long/_short.
# Но тот же хвост носят сокращения интерфейса (operatorMode_Turret_Short = 'TUR')
# и названия гоночных трасс (ea_ui_map_NHS_OldVanderval_Short), поэтому
# отсекаем по префиксу.
_LONGSHORT_RX = re.compile(r'^(long|short)$', re.I)


def _is_objective_longshort(key: str) -> bool:
    toks = tokenize(key)
    if len(toks) < 2 or not _LONGSHORT_RX.match(toks[-1]):
        return False
    return toks[0].lower() not in _NON_MISSION_PREFIXES


def _prefix(key: str) -> str:
    toks = tokenize(key)
    return toks[0].lower() if toks else ''


# ---------- 3. категории ----------

@dataclass
class Category:
    """Одна включаемая группа ключей."""
    id: str
    title: str            # человеческое название для интерфейса
    hint: str             # пояснение, что попадёт
    # Все матчеры принимают (ключ, значение). Значение нужно не всем,
    # но единая сигнатура избавляет classify() от разбора аргументов.
    match: Callable[[str, str], bool]
    enabled_by_default: bool = True
    # Категория может разрешить перевод названий (для журнала это осмысленно).
    allow_names: bool = False


# StarStrings подменяет несколько служебных строк главного экрана своими
# заметками: версию патча — на «MrKraken Community Translated Version», подпись
# кнопки «Играть» — на «Remember: Update StarStrings...», а описание вселенной —
# на инструкцию про обновление пака. Игроку, который поставил русификатор,
# это не нужно: он и так знает, откуда взял файл.
#
# Список именно перечислением, а не по маске: маска на frontend_* утащила бы
# заодно и обычные подписи кнопок, которые мы намеренно держим английскими.
_STARSTRINGS_NOTICE_KEYS = frozenset({
    'frontend_pu_version',              # «Alpha 4.10: ... | MrKraken Community Translated Version»
    'frontend_play_star_citizen',       # «Remember: Update StarStrings if you see broken text!!!»
    'ui_pregame_persistentuniverse_desc',  # абзац про mrkraken.space/starstrings
})


def _m_starstrings_notice(k: str, v: str = '') -> bool:
    """Служебные надписи StarStrings на главном экране."""
    # Хвост ',P' — вариант строки, к смыслу ключа не относится.
    return _SUFFIX_RX.sub('', k.strip()).lower() in _STARSTRINGS_NOTICE_KEYS


def _m_subtitles(k: str, v: str = '') -> bool:
    """Реплики NPC и субтитры. PU_ и PH_PU_ — озвученные диалоги, Dlg_ — диалоговые строки."""
    toks = _tokens_lower(k)
    # PU_ обычно значит реплику, но в PU_UEE_Navy_RepUI_Area это просто фракция,
    # а 'UEE' и 'Military' — справочные поля, а не диалог. Отдаём их org_desc.
    if 'repui' in toks:
        return False
    if toks and toks[0] in ('dlg', 'pu'):
        return True
    # PH_PU_GENOUTLAW1_... — тот же диалог, просто с префиксом PH
    if len(toks) >= 2 and toks[0] == 'ph' and toks[1] == 'pu':
        return True
    return 'subtitle' in ' '.join(toks) or any('subtitle' in t for t in toks)


def _m_voice(k: str, v: str = '') -> bool:
    """DXSH — реклама и голос комментатора арены (звучит вслух, идёт субтитрами)."""
    return _prefix(k) in ('dxsh', 'dxsm')


def _m_journal(k: str, v: str = '') -> bool:
    """Записи журнала и репутационные логи."""
    toks = _tokens_lower(k)
    if any('journal' in t for t in toks):
        return True
    # Короткая форма: 890_J_Mission_Obj_VIP_Long
    return bool(re.match(r'^\d+_J_', k))


def _m_mission_desc(k: str, v: str = '') -> bool:
    """Описания и брифинги миссий."""
    if not _has_desc_token(k):
        return False
    toks = _tokens_lower(k)
    if any('brief' in t for t in toks):
        return True
    return _prefix(k) not in _NON_MISSION_PREFIXES


# Игра сама делит цели по экранам через имя ключа:
#   _marker_ — метка в космосе, _short_ и _hud_ — трекер справа (всё это ХУД),
#   _long_   — панель контракта в мобигласе.
# Видно и по стилю: короткие в Title Case, длинные — предложения с точкой.
_HUD_TOKENS = {'hud', 'marker', 'short'}


def _is_hud_key(k: str) -> bool:
    return bool(_HUD_TOKENS & set(_tokens_lower(k)))


def _m_mission_obj(k: str, v: str = '') -> bool:
    """Цели в мобигласе: длинные формулировки в панели контракта."""
    if not (_has_obj_token(k) or _is_objective_longshort(k)):
        return False
    return not _is_hud_key(k)


def _m_mission_obj_hud(k: str, v: str = '') -> bool:
    """Цели на ХУДе: трекер справа и метки в космосе."""
    if not (_has_obj_token(k) or _is_objective_longshort(k) or _tokens_lower(k)[-1:] == ['marker']):
        return False
    return _is_hud_key(k) or _tokens_lower(k)[-1:] == ['marker']


def _m_mission_brief(k: str, v: str = '') -> bool:
    """MissionBriefs — устные брифинги от заказчиков."""
    return any('brief' in t.lower() for t in tokenize(k))


def _m_item_desc(k: str, v: str = '') -> bool:
    """Описания предметов и компонентов."""
    return _prefix(k) in ('item', 'items', 'itemport', 'port', 'scitem') and _has_desc_token(k)


def _m_vehicle_desc(k: str, v: str = '') -> bool:
    """Описания кораблей и наземной техники."""
    return _prefix(k) == 'vehicle' and _has_desc_token(k)


def _m_org_desc(k: str, v: str = '') -> bool:
    """
    Описания организаций и биографии контактов в окне репутации.

    Берём только прозу: RepUI_Description и RepUI_Biography. Соседние поля
    RepUI_Focus ('Mining & Local Jobs'), RepUI_Founded ('2943'), RepUI_Area,
    RepUI_Leadership, RepUI_Headquarters — это короткие справочные значения,
    ближе к названиям, их не трогаем.
    """
    toks = _tokens_lower(k)
    if 'repui' not in toks:
        return False
    return _has_desc_token(k) or any(t.startswith('bio') for t in toks)


# На главном экране почти всё — подписи кнопок ('Add Friends', 'Continue Last Save'),
# и переводить их не надо.
# '(?<!con)text' обязателен: токен 'Context' содержит 'text', и без этой
# оговорки кнопка Frontend_Context_AbleJoinContact ('Join Friend') уезжала в перевод.
_FRONTEND_PROSE_RX = re.compile(r'(explained|description|tooltip|message|warning|desc|(?<!con)text)', re.I)

# Описание — это предложение: есть пробел и точка (или длинное).
# Подпись кнопки предложением не является: 'Join Friend', 'Warning', 'Cannot Join (Server Full)'.
def _looks_like_sentence(value: str) -> bool:
    text = value.replace('\\n', ' ').strip()
    if ' ' not in text:
        return False
    return bool(re.search(r'[.!?]', text)) or len(text) > 60


def _m_frontend_desc(k: str, v: str = '') -> bool:
    """Описательный текст на главном экране: пояснения, предупреждения, подсказки."""
    if _prefix(k) not in ('frontend', 'mainmenu', 'launcher'):
        return False
    if not any(_FRONTEND_PROSE_RX.search(t) for t in tokenize(k)):
        return False
    # Без значения (например, из check_rules) считаем по ключу — иначе смотрим на текст.
    return _looks_like_sentence(v) if v else True


def _m_location_desc(k: str, v: str = '') -> bool:
    """
    Описания локаций на карте и экране выбора старта.

    Живут под префиксами ui_ и text_, которые целиком исключены (там подписи
    управления вроде 'Auto Targeting - Toggle On (Long Press)'), поэтому
    вытаскиваем их адресно, а не открытием всего ui_*.
    """
    toks = _tokens_lower(k)
    if toks[:3] == ['text', 'level', 'info'] and 'description' in toks:
        return True
    return toks[:3] == ['ui', 'pregame', 'port'] and _has_desc_token(k)


def _m_hints(k: str, v: str = '') -> bool:
    """Подсказки и обучающие тексты — длинные пояснения, не подписи кнопок."""
    return _prefix(k) == 'hints'


def _m_lore_text(k: str, v: str = '') -> bool:
    """Датапады, вывески, записки — лор, который читают с экранов."""
    toks = _tokens_lower(k)
    return any(t in ('body', 'bodytext', 'searchbody', 'fluff', 'flufftext', 'datapad') for t in toks)


# Префиксы, которые сами по себе не миссии — их описания разбираются своими
# категориями, а хвост _long/_short у них означает не цель, а сокращение
# ('operatorMode_Turret_Short' = 'TUR') или название ('ea_ui_map_..._Short').
_NON_MISSION_PREFIXES = {
    'ui', 'vehicle', 'item', 'items', 'itemport', 'port', 'scitem',
    'frontend', 'mainmenu', 'launcher', 'hints', 'notification',
    'mobiglas', 'menu', 'settings', 'options', 'keybinds', 'hud',
    'store', 'shop', 'chat', 'input', 'pause',
    'operatormode', 'mastermode', 'ea', 'dfm', 'text',
}


# Порядок важен: ключ приписывается ПЕРВОЙ совпавшей категории.
# Поэтому узкие категории идут раньше широких, а 'other_desc' — последним,
# иначе он забирал бы себе описания кораблей, предметов и организаций.
CATEGORIES: list[Category] = [
    # Первой: перебивает всё остальное, что могло бы зацепить эти три ключа.
    # Название начинается с глагола нарочно. «Служебные надписи StarStrings»
    # рядом с галочкой читались как «показывать их», хотя галочка значит
    # ровно обратное: взять перевод, то есть надписи убрать.
    Category('starstrings_notice', 'Убрать рекламу StarStrings',
             'Главный экран: версия, кнопка «Играть» и описание вселенной — '
             'обычным текстом вместо заметок MrKraken',
             _m_starstrings_notice),
    Category('journal', 'Записи в журнале',
             'Journal*, ReputationJournal*',
             _m_journal, allow_names=True),
    Category('subtitles', 'Субтитры и диалоги',
             'Dlg_*, PU_*, PH_PU_*, *subtitle*',
             _m_subtitles),
    Category('voice', 'Реклама и комментатор',
             'DXSH_* — озвученные объявления',
             _m_voice),
    Category('item_desc', 'Описания предметов и компонентов',
             'item_Desc*, включая item_DescARMR, item_Descarma',
             _m_item_desc),
    Category('vehicle_desc', 'Описания кораблей и техники',
             'vehicle_DescRSI_Meteor и подобные',
             _m_vehicle_desc),
    Category('org_desc', 'Описания организаций и биографии',
             'RepUI_Description и RepUI_Biography — лор фракций и контактов',
             _m_org_desc),
    Category('frontend_desc', 'Описания на главном экране',
             'Пояснения и предупреждения в меню, не подписи кнопок',
             _m_frontend_desc),
    Category('location_desc', 'Описания локаций',
             'Левски, Ариа18, Орисон — тексты на карте и экране выбора старта',
             _m_location_desc),
    Category('mission_brief', 'Брифинги миссий',
             'MissionBriefs — устная постановка задачи',
             _m_mission_brief),
    Category('mission_obj', 'Цели миссий (мобиглас)',
             'Длинные формулировки в панели контракта',
             _m_mission_obj),
    Category('mission_obj_hud', 'Цели миссий (ХУД)',
             'Трекер справа и метки в космосе. Выключено — на ХУДе английский',
             _m_mission_obj_hud, enabled_by_default=False),
    Category('hints', 'Подсказки и обучение',
             'Hints_* — длинные пояснения механик',
             _m_hints, enabled_by_default=False),
    Category('lore_text', 'Датапады, вывески, записки',
             '*_body, *_BodyText, *_Fluff*',
             _m_lore_text, enabled_by_default=False),
    Category('other_desc', 'Описания миссий и прочие тексты',
             'Любой ключ с токеном Desc, не попавший в категории выше',
             _m_mission_desc),
]

CATEGORY_BY_ID = {c.id: c for c in CATEGORIES}


def default_profile() -> dict[str, bool]:
    profile = {c.id: c.enabled_by_default for c in CATEGORIES}
    profile[FULL_ID] = False      # полный русский — выкл
    profile[ENGLISH_ID] = False   # английский с блюпринтами — выкл
    return profile


@dataclass
class Decision:
    """Результат разбора одного ключа — с объяснением, почему так."""
    translate: bool
    reason: str
    category: Optional[str] = None


# Особые режимы (не категории, а флаги в профиле):
#   full    — перевести всё, что переведено, включая интерфейс и названия;
#   english — вообще не переводить, поставить английский StarStrings с блюпринтами.
# english обрабатывается на уровне установки, до classify сюда не доходит.
FULL_ID = 'full'
ENGLISH_ID = 'english'


def classify(key: str, profile: dict[str, bool], en_value: str = '') -> Decision:
    """
    Решает, брать ли русский перевод для ключа.
    profile — какие категории включены; en_value нужен, чтобы отсеять [PH]-заглушки.
    """
    if is_hard_excluded(key):
        return Decision(False, 'технический ключ')

    if en_value and _PLACEHOLDER_VALUE_RX.match(en_value):
        return Decision(False, 'английский текст — заглушка [PH]')

    # Полный перевод: берём любой ключ, минуя категории и фильтр названий.
    # Есть русская строка — применяем; это забота merge, здесь лишь разрешаем.
    if profile.get(FULL_ID):
        return Decision(True, 'Полный перевод', FULL_ID)

    matched = [c for c in CATEGORIES if profile.get(c.id, False) and c.match(key, en_value)]
    if not matched:
        return Decision(False, 'не подходит ни под одну включённую категорию')

    # Названия не переводим — но у журнала заголовок это часть записи.
    if has_name_token(key) and not any(c.allow_names for c in matched):
        return Decision(False, f'название/заголовок ({matched[0].title})')

    cat = matched[0]
    return Decision(True, cat.title, cat.id)
