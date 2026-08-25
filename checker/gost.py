"""GOST 7.32-2017 compliance checker for PDF reports."""
import re

PT_PER_MM = 2.834645669
# Required margins (mm) per GOST 7.32 п.6.1.1
MARGIN_LEFT   = 30
MARGIN_RIGHT  = 15
MARGIN_TOP    = 20
MARGIN_BOTTOM = 20
# Допуск на заход текста в поле, мм. Слева и справа замер точный: строка
# начинается ровно на границе набора, на образце с полями 30/15 измеряется
# 30.0/15.0. Сверху и снизу мерить нечем, кроме чернил букв – над строкой
# остаётся просвет межстрочного интервала, а внизу мешают колонтитулы, поэтому
# там допуск вдвое шире.
MARGIN_TOL      = 3
MARGIN_TOL_VERT = 6
# Wider slack the other way: margins are measured by the ink extents of the
# glyphs, so paragraph spacing before a heading always reads as a larger
# margin than the page setup actually declares.
MARGIN_WIDE_TOL = 12

BODY_PT = 14.0        # running text is exactly 14 pt
PT_TOL  = 0.5         # rounding slack when reading sizes out of the PDF


class Check:
    def __init__(self, code: str, name: str, passed: bool,
                 details: str = '', severity: str = 'error'):
        self.code = code
        self.name = name
        self.passed = passed
        self.details = details
        self.severity = severity  # 'error' | 'warning'

    def to_dict(self):
        return {
            'code': self.code,
            'name': self.name,
            'passed': self.passed,
            'details': self.details,
            'severity': self.severity,
        }


# Canonical list of all checks (code, name, group, hint) – single source of
# truth for the UI checkboxes. Keep the codes/names in sync with the Check()
# instances created below. 'structure' = структурные элементы, 'format' = оформление.
GOST_CHECKS = [
    ('S1', 'Титульный лист', 'structure',
     'Организация, тема, исполнитель, город и год.'),
    ('S2', 'Задание на практику или курсовую', 'structure',
     'Отдельный лист «Задание на практику / на курсовую работу».'),
    ('S3', 'Реферат', 'structure',
     'Обязателен для отчёта о НИР (ГОСТ 7.32-2017 п.5.3) и для ВКР '
     '(ГОСТ Р 7.0.11-2011). Для отчёта по практике и курсовой работы обычно '
     'не требуется – снимите критерий, если он не нужен.'),
    ('S4', 'Содержание', 'structure',
     'Заголовок «СОДЕРЖАНИЕ» и перечень разделов с номерами страниц.'),
    ('S5', 'Введение и актуальность', 'structure',
     'Раздел «ВВЕДЕНИЕ» на месте, и в нём названа актуальность работы. '
     'Отчёт, который введения не предполагает, проверять по этому критерию '
     'не нужно – снимите его, цель и задачи останутся под S5A.'),
    ('S5A', 'Цель, задачи, ход работы', 'structure',
     'Цель и задачи работы. Ищутся во введении, а если введения нет – на '
     'первых страницах до первой главы, поэтому критерий работает и без S5. '
     '«Ход работы» отдельно не проверяется: автоматически его не отличить от '
     'пересказа содержания.'),
    ('S6', 'Главы (нумерованные разделы)', 'structure',
     'Разделы вида «1 Название» – без точки после номера.'),
    ('S7', 'Подглавы (подразделы)', 'structure',
     'Подразделы вида «1.1 Название».'),
    ('S8', 'Заключение', 'structure',
     'Заголовок «ЗАКЛЮЧЕНИЕ» с выводами по работе.'),
    ('S9', 'Список использованных источников', 'structure',
     'Так этот раздел называется в ГОСТ 7.32-2017 п.5.10. Варианты '
     '«Список литературы» и «Библиография» тоже засчитываются.'),

    ('F1', 'Нумерация страниц', 'format',
     'Арабские цифры, сквозная нумерация, титульный лист не нумеруется.'),
    ('F2', 'Шрифт Times New Roman', 'format',
     'Проверяется по всему документу, включая титульный лист и задание.'),
    ('F3', 'Основной текст 14 пт', 'format',
     'Ровно 14 пт. Титульный лист и задание не учитываются – на них '
     'проверяется только гарнитура.'),
    ('F4', 'Текст рисунков и таблиц не больше 14 пт', 'format',
     'Подписи под рисунками и текст внутри таблиц могут быть мельче основного, '
     'но не крупнее 14 пт.'),
    ('F5', 'Шрифт номеров страниц', 'format',
     'Номер страницы тоже набирается Times New Roman.'),
    ('F6', 'Подписи рисунков', 'format',
     'Формат «Рисунок 1 – Название», без сокращения «Рис.» и без знака №.'),
    ('F7', 'Ссылки на рисунки в тексте', 'format',
     'На каждый рисунок должна быть ссылка в тексте: «на рисунке 1 показано…».'),
    ('F8', 'Подписи таблиц', 'format',
     'Формат «Таблица 1 – Название», без сокращения «Табл.».'),
    ('F9', 'Точки в конце заголовков', 'format',
     'Заголовок разделов и подразделов точкой не завершают.'),
    ('F10', 'Ссылки на источники [N]', 'format',
     'В тексте ссылки в квадратных скобках: [7].'),
    ('F11', 'Поля страницы', 'format',
     'Левое 30, правое 15, верхнее и нижнее 20 мм. Титульный лист и задание '
     'не измеряются: на них текст обычно выровнен по центру.'),
    ('F12', 'Прописные заголовки структурных элементов', 'format',
     'Заголовки «РЕФЕРАТ», «СОДЕРЖАНИЕ», «ВВЕДЕНИЕ», «ЗАКЛЮЧЕНИЕ» и '
     'списка источников набирают прописными буквами '
     '(ГОСТ 7.32-2017 п.6.2.2).'),
]

ALL_CODES = [c[0] for c in GOST_CHECKS]
CHECK_NAMES = {c[0]: c[1] for c in GOST_CHECKS}
CHECK_HINTS = {c[0]: c[3] for c in GOST_CHECKS}

# Те же нарушения, но названные так, как их пишут в замечании студенту:
# коротко, в лицо работе, без ссылок на пункты стандарта. Отсюда собирается
# готовый отзыв, который преподаватель копирует на портал.
FLAW_TEXT = {
    'S1':  'Титульный лист оформлен не по требованиям',
    'S2':  'Отсутствует лист задания на практику (курсовую работу)',
    'S3':  'Отсутствует реферат',
    'S4':  'Отсутствует содержание',
    'S5':  'Отсутствует введение или в нём не названа актуальность работы',
    'S5A': 'Не указаны цель и задачи работы',
    'S6':  'Работа не разбита на нумерованные главы',
    'S7':  'Главы не разбиты на подглавы',
    'S8':  'Отсутствует заключение',
    'S9':  'Отсутствует список использованных источников',
    'F1':  'Отсутствует нумерация страниц',
    'F2':  'Неверный шрифт – требуется Times New Roman',
    'F3':  'Неверный размер основного текста – требуется 14 пт',
    'F4':  'Текст подписей и таблиц крупнее 14 пт',
    'F5':  'Номера страниц набраны не шрифтом Times New Roman',
    'F6':  'Неверные подписи рисунков – требуется «Рисунок 1 – Название»',
    'F7':  'В тексте нет ссылок на рисунки',
    'F8':  'Неверные заголовки таблиц – требуется «Таблица 1 – Название»',
    'F9':  'Точка в конце заголовка',
    'F10': 'Нет ссылок на источники в квадратных скобках – [1]',
    'F11': 'Неверные поля страницы – левое 30, правое 15, верхнее и нижнее 20 мм',
    'F12': 'Заголовки структурных элементов должны быть набраны прописными буквами',
}


# ─────────────────────────  Shared text helpers  ─────────────────────────

_CONTENTS_HEADING = r'(?:СОДЕРЖАНИЕ|ОГЛАВЛЕНИЕ)'
_REFERENCES_HEADING = (
    r'(?:СПИСОК\s+(?:ИСПОЛЬЗОВАННЫХ\s+|ИСПОЛЬЗУЕМЫХ\s+)?'
    r'(?:ИСТОЧНИКОВ|ЛИТЕРАТУРЫ|ССЫЛОК)|БИБЛИОГРАФИЯ|ЛИТЕРАТУРА)'
)
_STRUCTURAL_HEADING = (
    rf'(?:РЕФЕРАТ|{_CONTENTS_HEADING}|ВВЕДЕНИЕ|ЗАКЛЮЧЕНИЕ|'
    rf'{_REFERENCES_HEADING})'
)


def _line_heading_matches(text: str, heading: str,
                          allow_dot: bool = False) -> list:
    """Standalone heading matches, preserving the spelling found in the PDF."""
    ending = r'\.?' if allow_dot else ''
    return list(re.finditer(
        rf'^[^\S\r\n]*(?:{heading}){ending}[^\S\r\n]*$',
        text or '', re.MULTILINE | re.IGNORECASE,
    ))


def _is_contents_page(text: str) -> bool:
    # The heading is normally the first line; limiting the prefix avoids
    # excluding a body page that merely mentions the table of contents.
    return bool(_line_heading_matches(
        (text or '')[:500], _CONTENTS_HEADING, allow_dot=True))


def _is_front(meta: dict) -> bool:
    """Лист титульного раздела: титул, задание или его продолжение.

    Продолжение приходит из разбора отдельным признаком (`is_front`): своей
    приметы в тексте у него нет, а рамка и шрифт на нём титульные, и от
    проверок он освобождён наравне с самим заданием.
    """
    return bool(meta.get('is_title') or meta.get('is_task')
                or meta.get('is_front'))


def _ordinary_pages(report: dict) -> list:
    """Page metadata without the title page, the «задание» sheet and the
    table of contents – the pages whose headings and margins are meaningful."""
    pages = report.get('pages') or []
    texts = report.get('text_by_page') or []
    out = []
    for meta in pages:
        idx = meta['page'] - 1
        text = texts[idx] if idx < len(texts) else ''
        if _is_front(meta):
            continue
        if _is_contents_page(text):
            continue
        out.append(meta)
    return out


def _non_front_page_texts(report: dict) -> list:
    """``(page, text)`` pairs without title and task sheets."""
    texts = report.get('text_by_page') or []
    pages = report.get('pages') or []
    if texts:
        metadata = {m.get('page'): m for m in pages}
        out = []
        for page, text in enumerate(texts, 1):
            meta = metadata.get(page, {})
            if _is_front(meta):
                continue
            out.append((page, text))
        return out

    full_text = report.get('full_text', '')
    return [(1, full_text)] if full_text else []


def _ordinary_page_texts(report: dict) -> list:
    """``(page, text)`` pairs without title, task and contents pages."""
    return [(page, text) for page, text in _non_front_page_texts(report)
            if not _is_contents_page(text)]


def _body_text(report: dict) -> str:
    """Text of the ordinary pages. Excluding the contents page matters: its
    lines look exactly like headings and would inflate every heading count."""
    return '\n'.join(text for _, text in _ordinary_page_texts(report))


def _heading_text(report: dict, heading: str, body: bool = True) -> str:
    """Last standalone occurrence of a heading, or an empty string."""
    text = _body_text(report) if body else report.get('full_text', '')
    matches = _line_heading_matches(text, heading, allow_dot=True)
    return matches[-1].group(0).strip() if matches else ''


def _section_text(text: str, heading: str) -> str:
    """Text from the last heading line up to the next structural heading.

    The last occurrence, not the first: in a document that still contains its
    table of contents the first «ВВЕДЕНИЕ» is the contents line, and slicing
    from there returns the rest of the contents instead of the section.
    """
    matches = _line_heading_matches(text, heading, allow_dot=True)
    if not matches:
        return ''
    rest = text[matches[-1].end():]
    known = _line_heading_matches(rest, _STRUCTURAL_HEADING, allow_dot=True)
    caps = list(re.finditer(
        r'^[^\S\r\n]*[А-ЯЁA-Z][А-ЯЁA-Z \t]{5,}[^\S\r\n]*$',
        rest, re.MULTILINE,
    ))
    starts = [m.start() for m in known[:1] + caps[:1]]
    return rest[:min(starts)] if starts else rest[:4000]


def _before_references(text: str) -> str:
    """Everything up to the bibliography. Its entries («1. ГОСТ 7.32-2017.»)
    look exactly like numbered headings ending in a dot."""
    m = re.search(rf'\b{_REFERENCES_HEADING}\b', text, re.IGNORECASE)
    return text[:m.start()] if m else text


_FIRST_CHAPTER_RE = re.compile(
    r'^[^\S\r\n]*(?:\d+\.?[ \t]+[А-ЯЁA-Z]|(?:ГЛАВА|Глава)[ \t]+\d+)',
    re.MULTILINE,
)
_INTRO_END_HEADING = r'ЗАКЛЮЧЕНИЕ'


def _before_first_chapter(text: str) -> str:
    """Вводная часть работы – всё до первой главы или до заключения."""
    starts = [m.start() for m in _FIRST_CHAPTER_RE.finditer(text)]
    starts += [m.start() for m in
               _line_heading_matches(text, _INTRO_END_HEADING, allow_dot=True)]
    return text[:min(starts)] if starts else text


def _intro_scope(report: dict) -> str:
    """Текст, в котором ищутся цель и задачи работы.

    Обычно это раздел «ВВЕДЕНИЕ». Но отчёт по практике часто обходится без
    него, а цель и задачи в нём всё равно названы – на первых страницах перед
    первой главой. Без этого запасного варианта работа без введения проваливала
    бы оба введенческих критерия сразу, а разбивали их как раз ради обратного.
    """
    body = _body_text(report)
    if _line_heading_matches(body, 'ВВЕДЕНИЕ', allow_dot=True):
        return _section_text(body, 'ВВЕДЕНИЕ')
    return _before_first_chapter(_before_references(body))


def _body_pages_before_references(report: dict) -> list:
    """Ordinary page texts, truncated at the references section."""
    out = []
    for page, text in _ordinary_page_texts(report):
        m = re.search(rf'\b{_REFERENCES_HEADING}\b', text, re.IGNORECASE)
        out.append((page, text[:m.start()] if m else text))
        if m:
            break
    return out


_TOP_CHAPTER_RE = re.compile(
    r'^(\d+)(\.)?[ \t]+[А-ЯЁA-Z][^\n]{2,}$'
)
_NUMBERED_ITEM_RE = re.compile(r'^(\d+)\.[ \t]+\S.{2,}$')
_NAMED_CHAPTER_RE = re.compile(r'^ГЛАВА[ \t]+(\d+)\b', re.IGNORECASE)


def _chapter_lines(report: dict) -> list:
    """Numbered-line context used by both S6 and F9.

    A top-level chapter must have running text later on the same page. Adjacent
    ``N. ...`` lines whose numbers increase by one are a list, not headings.
    """
    all_lines = []

    for page, text in _body_pages_before_references(report):
        page_lines = []
        for index, line in enumerate(text.splitlines()):
            stripped = line.strip()
            if not stripped or re.fullmatch(r'\d+', stripped):
                continue
            chapter = _TOP_CHAPTER_RE.match(stripped)
            item = _NUMBERED_ITEM_RE.match(stripped)
            named = _NAMED_CHAPTER_RE.match(stripped)
            rec = {
                'key': (page, index),
                'page': page,
                'line': stripped,
                'chapter': chapter,
                'item': item,
                'named': named,
                'has_text_after': False,
                'in_list': False,
            }
            page_lines.append(rec)
            all_lines.append(rec)

        has_running_text = False
        for rec in reversed(page_lines):
            rec['has_text_after'] = has_running_text
            is_other_heading = bool(
                rec['chapter'] or rec['item'] or rec['named']
                or re.fullmatch(r'\d+(?:\.\d+)+\.?[ \t]+\S.+', rec['line'])
                or _line_heading_matches(
                    rec['line'], _STRUCTURAL_HEADING, allow_dot=True)
            )
            if not is_other_heading:
                has_running_text = True

    # Blanks and page numbers do not break a list; ordinary extracted text does.
    # Treating arbitrary text as a wrapped item would hide real short chapters.
    # The flattened order still catches adjacent items across a page boundary.
    for first, second in zip(all_lines, all_lines[1:]):
        a, b = first['item'], second['item']
        if a and b and int(b.group(1)) == int(a.group(1)) + 1:
            first['in_list'] = True
            second['in_list'] = True

    return all_lines


def _font_totals(counts: dict) -> tuple:
    """(total characters, characters set in Times New Roman)."""
    total = tnr = 0
    for (fname, _), n in counts.items():
        total += n
        if 'times' in fname.lower().replace('-', '').replace(' ', ''):
            tnr += n
    return total, tnr


def _size_totals(counts: dict) -> dict:
    sizes: dict = {}
    for (_, size), n in counts.items():
        if size and size > 0:
            sizes[size] = sizes.get(size, 0) + n
    return sizes


# ─────────────────────────  Entry point  ─────────────────────────

def check_gost(report: dict, enabled=None) -> list:
    """Run all GOST checks, optionally keeping only a subset.

    enabled: iterable of check codes (e.g. {'S1', 'F7'}) to include. When None
    (the default) every check is returned, preserving prior behaviour.
    """
    checks = [
        _title_page(report),
        _task_sheet(report),
        _abstract(report),
        _toc(report),
        _introduction(report),
        _introduction_goals(report),
        _chapters(report),
        _subchapters(report),
        _conclusion(report),
        _references(report),
        _page_numbers(report),
        _font_family(report),
        _body_font_size(report),
        _aux_font_size(report),
        _pagenum_font(report),
        _figure_captions(report),
        _figure_mentions(report),
        _table_captions(report),
        _heading_no_dot(report),
        _cite_format(report),
        _margins(report),
        _uppercase_structural_headings(report),
    ]
    results = [c.to_dict() for c in checks]
    if enabled is not None:
        allow = set(enabled)
        results = [c for c in results if c['code'] in allow]
    return results


# ─────────────────────────  Structure  ─────────────────────────

# Организацию на титульном листе называют по-разному: полным именем
# («федеральное государственное бюджетное образовательное учреждение высшего
# образования»), строкой министерства («МИНОБРНАУКИ РОССИИ») или одной
# аббревиатурой с именем («РГУ нефти и газа (НИУ) имени И.М. Губкина»).
# В последних двух случаях слова «университет» в тексте нет вовсе, и титул,
# свёрстанный ровно по бланку вуза, объявлялся неполным.
_ORG_RE = re.compile(
    r'федеральн|министерств|минобрнауки|минобразовани|минпросвещени'
    r'|университет|институт|академи|учреждени|колледж|техникум'
    # «имени И.М. Губкина», «им. Н.Э. Баумана» – так подписаны вузы, чьё
    # название стоит на титуле только аббревиатурой.
    r'|\bим(?:ени)?\.?\s*[А-ЯЁ]\.\s*[А-ЯЁ]\.',
    re.IGNORECASE)
# Аббревиатуры разбираются отдельно и с учётом регистра: со снятым под шаблон
# попало бы обычное слово («РАБОТУ»). Третья буква формы собственности –
# бюджетное, автономное, казённое, унитарное, – заодно отсекает «ФГОС»,
# который стоит в задании и организацией не является. Список вузов открытый:
# как и список городов, он пополняется по мере того, как встречаются бланки
# других учебных заведений.
_ORG_ABBR_RE = re.compile(
    r'\bФГ[АБКУ][А-ЯЁ]{1,2}\b'                  # ФГБОУ ВО, ФГАОУ, ФГБУН, ФГУП
    r'|\b(?:НИУ|НИЯУ|МГУ|МГТУ|РГУ|СПбГУ|СПбПУ|РУДН|ВШЭ|ИТМО|МФТИ|МИФИ'
    r'|МИСиС|МАИ|МЭИ|МАДИ|МИЭТ|ГУАП|ЛЭТИ)\b')
# Год в подвале титульного листа чаще всего вписан в бланк «Москва, 20__»:
# две последние цифры ставит студент, а на месте пропуска остаётся линейка.
# Из PDF такая строка выходит как «Москва, 20 25» или «Москва, 20__2_5___»,
# поэтому четырёх цифр подряд в ней искать нельзя – между ними допускаются
# пробелы и подчёркивания, но не буквы: «20 25» – год, «20 мая 25-го» – нет.
# Незаполненный бланк «Москва, 20__» под шаблон не подходит, и это верно:
# года на таком титуле действительно нет.
_YEAR_RE = re.compile(r'(?<!\d)20[\s_]{0,8}\d[\s_]{0,8}\d(?!\d)')
_AUTHOR_RE = re.compile(r'выполнил|студент|автор', re.IGNORECASE)
_CITY_RE = re.compile(
    r'Москва|Санкт-Петербург|Екатеринбург|Новосибирск|Казань|Самара'
    r'|Алматы|Алма-Ата|Астана|Нур-Султан|Шымкент|Семей|Атырау|Актобе'
    r'|\bМ\.\b')


def _title_page(report: dict) -> Check:
    """п.5.1, Титульный лист."""
    text_by_page = report.get('text_by_page', [])
    title_text = '\n'.join(text_by_page[:2]) if text_by_page else ''

    has_org = bool(_ORG_RE.search(title_text)
                   or _ORG_ABBR_RE.search(title_text))
    has_year = bool(_YEAR_RE.search(title_text))
    has_author = bool(_AUTHOR_RE.search(title_text))
    has_city = bool(_CITY_RE.search(title_text))

    found = [
        ('наименование организации', has_org),
        ('ФИО исполнителя', has_author),
        ('год составления', has_year),
        ('место составления', has_city),
    ]
    missing = [name for name, ok in found if not ok]
    score = 4 - len(missing)

    if score >= 3:
        return Check('S1', 'Титульный лист', True,
                     'Найден, все обязательные сведения на месте' if not missing
                     else 'Найден, не хватает: ' + ', '.join(missing))
    if score >= 2:
        return Check('S1', 'Титульный лист', False,
                     f'Неполный, отсутствует: {", ".join(missing)}', 'warning')
    return Check('S1', 'Титульный лист', False,
                 'Титульный лист не обнаружен (ГОСТ 7.32 п.5.1)')


def _task_sheet(report: dict) -> Check:
    """Задание на практику / на курсовую работу – отдельный лист."""
    pages = [m['page'] for m in (report.get('pages') or []) if m.get('is_task')]
    if pages:
        return Check('S2', 'Задание на практику или курсовую', True,
                     f'Лист задания найден: стр. {pages[0]}')
    if re.search(r'\bЗАДАНИЕ\b', report.get('full_text', ''), re.IGNORECASE):
        return Check('S2', 'Задание на практику или курсовую', False,
                     'Слово «задание» встречается, но отдельного листа задания '
                     'на практику или курсовую работу не найдено', 'warning')
    return Check('S2', 'Задание на практику или курсовую', False,
                 'Лист задания не обнаружен')


def _abstract(report: dict) -> Check:
    """п.5.3, Реферат. Обязателен для отчёта о НИР и ВКР."""
    if not _heading_text(report, 'РЕФЕРАТ'):
        return Check('S3', 'Реферат', False,
                      'Структурный элемент «РЕФЕРАТ» отсутствует (ГОСТ 7.32 п.5.3). '
                      'Для отчёта по практике он обычно не требуется – критерий '
                     'можно снять', 'warning')

    body = _section_text(_body_text(report), 'РЕФЕРАТ')
    has_volume = bool(re.search(r'\b\d+\s*(с\.|страниц)', body, re.IGNORECASE))
    has_keywords = bool(re.search(r'ключевые\s+слова', body, re.IGNORECASE))
    missing = []
    if not has_volume:   missing.append('сведения об объёме работы')
    if not has_keywords: missing.append('перечень ключевых слов')
    if missing:
        return Check('S3', 'Реферат', False,
                     'Реферат найден, но в нём нет: ' + ', '.join(missing) +
                     ' (ГОСТ 7.32 п.5.3.2)', 'warning')
    return Check('S3', 'Реферат', True, 'Реферат с объёмом и ключевыми словами')


def _toc(report: dict) -> Check:
    """п.5.4, Содержание."""
    full_text = report.get('full_text', '')
    if not _heading_text(report, _CONTENTS_HEADING, body=False):
        return Check('S4', 'Содержание', False,
                      'Структурный элемент «СОДЕРЖАНИЕ» отсутствует (ГОСТ 7.32 п.5.4)')
    body = _section_text(full_text, _CONTENTS_HEADING)
    with_pages = re.findall(r'\.{2,}\s*\d+\s*$|\s\d+\s*$', body, re.MULTILINE)
    if len(with_pages) < 3:
        return Check('S4', 'Содержание', False,
                     'Содержание есть, но номера страниц у разделов не найдены',
                     'warning')
    return Check('S4', 'Содержание', True,
                 f'Разделов с номерами страниц: {len(with_pages)}')


def _introduction(report: dict) -> Check:
    """п.5.7, первая половина: сам раздел «ВВЕДЕНИЕ» и актуальность.

    Отделено от цели и задач намеренно: проверяются и отчёты, где введения или
    актуальности нет по самому жанру работы. Такой критерий преподаватель
    снимает целиком, а цель и задачи продолжает считать по S5A.
    """
    if not _heading_text(report, 'ВВЕДЕНИЕ'):
        return Check('S5', 'Введение и актуальность', False,
                      'Структурный элемент «ВВЕДЕНИЕ» отсутствует (ГОСТ 7.32 п.5.7)')

    body = _section_text(_body_text(report), 'ВВЕДЕНИЕ')
    if not re.search(r'актуальн', body, re.IGNORECASE):
        return Check('S5', 'Введение и актуальность', False,
                     'Введение есть, но актуальность работы в нём не названа',
                     'warning')
    return Check('S5', 'Введение и актуальность', True,
                 'Введение на месте, актуальность названа')


def _introduction_goals(report: dict) -> Check:
    """п.5.7, вторая половина: цель и задачи работы.

    Ищутся во введении, а без введения – во вводной части до первой главы:
    критерий не должен зависеть от того, озаглавлен ли этот кусок текста.
    Отдельного подпункта «ход работы» здесь нет – от пересказа содержания его
    не отличить, поэтому он остался только в названии критерия.
    """
    scope = _intro_scope(report)
    parts = {
        'цель':   r'\bцел[ьи]\b',
        'задачи': r'\bзадач',
    }
    missing = [label for label, pat in parts.items()
               if not re.search(pat, scope, re.IGNORECASE)]
    if missing:
        return Check('S5A', 'Цель, задачи, ход работы', False,
                     'В работе не найдено: ' + ', '.join(missing), 'warning')
    return Check('S5A', 'Цель, задачи, ход работы', True,
                 'Цель и задачи названы')


def _chapters(report: dict) -> Check:
    """п.6.4, Главы – нумерованные разделы «1 Название»."""
    lines = _chapter_lines(report)
    candidates = [rec for rec in lines
                  if rec['has_text_after'] and not rec['in_list']]
    numbered = [rec['chapter'].group(1) for rec in candidates
                if rec['chapter'] and not rec['chapter'].group(2)]
    named = [rec['named'].group(1) for rec in candidates if rec['named']]
    dotted = [rec['chapter'].group(1) for rec in candidates
              if rec['chapter'] and rec['chapter'].group(2)]

    # Distinct numbers, so a chapter written both ways is not counted twice.
    total = len(set(numbered) | set(named))
    if dotted:
        return Check('S6', 'Главы (нумерованные разделы)', False,
                     f'После номера главы стоит точка ({dotted[0]}.) – '
                     f'не допускается (ГОСТ 7.32 п.6.4.1). Найдено: {len(dotted)}',
                     'warning')
    if total == 0:
        return Check('S6', 'Главы (нумерованные разделы)', False,
                     'Нумерованные главы не обнаружены (ГОСТ 7.32 п.6.4)')
    return Check('S6', 'Главы (нумерованные разделы)', True, f'Глав: {total}')


def _subchapters(report: dict) -> Check:
    """п.6.4, Подглавы – подразделы «1.1 Название»."""
    text = _before_references(_body_text(report))
    subs = re.findall(r'^\s*(\d+\.\d+)\s+[А-ЯЁA-Za-zА-яё][^\n]{2,}$', text, re.MULTILINE)
    dotted = re.findall(r'^\s*(\d+\.\d+)\.\s+[А-ЯЁA-Za-zА-яё]', text, re.MULTILINE)

    if dotted and not subs:
        return Check('S7', 'Подглавы (подразделы)', False,
                     f'После номера подраздела стоит точка ({dotted[0]}.) – '
                     f'не допускается (ГОСТ 7.32 п.6.4.1)', 'warning')
    if not subs:
        return Check('S7', 'Подглавы (подразделы)', False,
                     'Подразделы вида «1.1 Название» не обнаружены', 'warning')
    return Check('S7', 'Подглавы (подразделы)', True,
                 f'Подразделов: {len(set(subs))}')


def _conclusion(report: dict) -> Check:
    """п.5.9, Заключение."""
    if _heading_text(report, 'ЗАКЛЮЧЕНИЕ'):
        return Check('S8', 'Заключение', True)
    return Check('S8', 'Заключение', False,
                 'Структурный элемент «ЗАКЛЮЧЕНИЕ» отсутствует (ГОСТ 7.32 п.5.9)')


def _references(report: dict) -> Check:
    """п.5.10, Список использованных источников."""
    full_text = report.get('full_text', '')
    m = re.search(
        r'\b(СПИСОК\s+(?:ИСПОЛЬЗОВАННЫХ\s+|ИСПОЛЬЗУЕМЫХ\s+)?'
        r'(?:ИСТОЧНИКОВ|ЛИТЕРАТУРЫ|ССЫЛОК)|БИБЛИОГРАФИЯ|ЛИТЕРАТУРА)\b',
        full_text, re.IGNORECASE)
    if not m:
        return Check('S9', 'Список использованных источников', False,
                     'Список использованных источников отсутствует '
                     '(ГОСТ 7.32 п.5.10)', 'warning')

    body = full_text[m.end():]
    entries = re.findall(r'^\s*\d+[.)]\s+\S', body, re.MULTILINE)
    if len(entries) < 3:
        return Check('S9', 'Список использованных источников', False,
                     f'Раздел найден, но пронумерованных источников в нём '
                     f'{len(entries)} – проверьте оформление', 'warning')
    return Check('S9', 'Список использованных источников', True,
                 f'Источников: {len(entries)}')


# ─────────────────────────  Formatting  ─────────────────────────

def _page_numbers(report: dict) -> Check:
    """п.6.3, Нумерация страниц арабскими цифрами."""
    text_by_page = report.get('text_by_page', [])
    total_pages = report.get('pages_count', 0)
    if total_pages <= 1:
        return Check('F1', 'Нумерация страниц', True, 'Документ одностраничный')

    found = 0
    for text in text_by_page[1:]:
        lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
        for line in (lines[:1] + lines[-1:]):
            if re.match(r'^\d+$', line) and 2 <= int(line) <= 999:
                found += 1
                break

    body_pages = total_pages - 1
    ratio = found / body_pages if body_pages else 1

    if ratio >= 0.5:
        return Check('F1', 'Нумерация страниц', True,
                     f'Номера найдены на {found}/{body_pages} страницах')
    if ratio >= 0.2:
        return Check('F1', 'Нумерация страниц', False,
                     f'Нумерация нерегулярна: {found}/{body_pages} страниц', 'warning')
    return Check('F1', 'Нумерация страниц', False,
                 'Нумерация страниц не обнаружена (ГОСТ 7.32 п.6.3)')


def _font_family(report: dict) -> Check:
    """п.6.1.1, Times New Roman во всём документе, включая титул и задание."""
    counts = (report.get('font_info') or {}).get('all') or {}
    if not counts:
        return Check('F2', 'Шрифт Times New Roman', False,
                     'Данные о шрифтах недоступны', 'warning')

    total, tnr = _font_totals(counts)
    if total == 0:
        return Check('F2', 'Шрифт Times New Roman', False,
                     'Символов не найдено', 'warning')
    ratio = tnr / total

    if ratio >= 0.9:
        return Check('F2', 'Шрифт Times New Roman', True,
                     f'Times New Roman: {ratio:.0%} символов')
    if ratio >= 0.7:
        return Check('F2', 'Шрифт Times New Roman', False,
                     f'Times New Roman в {ratio:.0%} символов – часть текста '
                     f'набрана другой гарнитурой (ГОСТ 7.32 п.6.1.1)', 'warning')

    totals: dict = {}
    for (fname, _), n in counts.items():
        totals[fname] = totals.get(fname, 0) + n
    names = ', '.join(f[0][:25] for f in sorted(totals.items(), key=lambda x: -x[1])[:3])
    return Check('F2', 'Шрифт Times New Roman', False,
                 f'Times New Roman лишь в {ratio:.0%} символов. Используется: '
                 f'{names}. Требуется TNR (ГОСТ 7.32 п.6.1.1)')


def _body_font_size(report: dict) -> Check:
    """Основной текст ровно 14 пт (титул и задание не учитываются)."""
    counts = (report.get('font_info') or {}).get('body') or {}
    sizes = _size_totals(counts)
    if not sizes:
        return Check('F3', 'Основной текст 14 пт', False,
                     'Размеры шрифта основного текста не определены', 'warning')

    total = sum(sizes.values())
    ok = sum(n for s, n in sizes.items() if abs(s - BODY_PT) <= PT_TOL)
    ratio = ok / total
    dominant = max(sizes, key=sizes.get)

    if ratio >= 0.9:
        return Check('F3', 'Основной текст 14 пт', True,
                     f'14 пт: {ratio:.0%} основного текста')
    others = ', '.join(f'{s:g} пт – {n / total:.0%}'
                       for s, n in sorted(sizes.items(), key=lambda x: -x[1])[:3]
                       if abs(s - BODY_PT) > PT_TOL)
    if ratio >= 0.7:
        return Check('F3', 'Основной текст 14 пт', False,
                     f'14 пт только в {ratio:.0%} текста. Также встречается: '
                     f'{others} (ГОСТ 7.32 п.6.1.1)', 'warning')
    return Check('F3', 'Основной текст 14 пт', False,
                 f'Основной размер {dominant:g} пт вместо 14 пт. '
                 f'Распределение: {others} (ГОСТ 7.32 п.6.1.1)')


def _aux_font_size(report: dict) -> Check:
    """Подписи рисунков и текст таблиц: допускается 14 пт и мельче."""
    counts = (report.get('font_info') or {}).get('aux') or {}
    sizes = _size_totals(counts)
    if not sizes:
        return Check('F4', 'Текст рисунков и таблиц не больше 14 пт', True,
                     'Подписей и таблиц не обнаружено')

    total = sum(sizes.values())
    too_big = {s: n for s, n in sizes.items() if s - BODY_PT > PT_TOL}
    if not too_big:
        smallest = min(sizes)
        return Check('F4', 'Текст рисунков и таблиц не больше 14 пт', True,
                     f'Размеры от {smallest:g} до 14 пт')

    share = sum(too_big.values()) / total
    listed = ', '.join(f'{s:g} пт' for s in sorted(too_big, reverse=True)[:3])
    return Check('F4', 'Текст рисунков и таблиц не больше 14 пт', False,
                 f'Крупнее 14 пт: {listed} – {share:.0%} текста подписей и таблиц',
                 'warning' if share < 0.3 else 'error')


def _pagenum_font(report: dict) -> Check:
    """Номера страниц тоже набираются Times New Roman."""
    counts = (report.get('font_info') or {}).get('pagenum') or {}
    if not counts:
        return Check('F5', 'Шрифт номеров страниц', False,
                     'Номера страниц не найдены – гарнитуру проверить нельзя',
                     'warning')

    total, tnr = _font_totals(counts)
    if total == 0:
        return Check('F5', 'Шрифт номеров страниц', False,
                     'Номера страниц не найдены', 'warning')
    ratio = tnr / total
    if ratio >= 0.9:
        return Check('F5', 'Шрифт номеров страниц', True,
                     'Номера страниц набраны Times New Roman')

    names = ', '.join(sorted({f for (f, _) in counts})[:3])
    return Check('F5', 'Шрифт номеров страниц', False,
                 f'Номера страниц набраны другой гарнитурой: {names}. '
                 f'Требуется Times New Roman (ГОСТ 7.32 п.6.1.1)')


def _figure_captions(report: dict) -> Check:
    """п.6.5, Подписи рисунков должны быть «Рисунок N»."""
    full_text = report.get('full_text', '')
    has_fig_refs = bool(re.search(r'рисун|рис\.|fig\.?', full_text, re.IGNORECASE))
    if not has_fig_refs:
        return Check('F6', 'Подписи рисунков', True, 'Рисунки не обнаружены')

    good   = re.findall(r'\bРисунок\s+\d+', full_text)
    bad_r  = re.findall(r'\bРис\.\s*\d+', full_text)
    bad_e  = re.findall(r'\b(?:Fig\.|Figure)\s*\d+', full_text, re.IGNORECASE)
    bad_nr = re.findall(r'\bрисунок\s+№\s*\d+', full_text, re.IGNORECASE)

    issues = []
    if bad_r:  issues.append(f'«Рис.» вместо «Рисунок» ({len(bad_r)} шт.)')
    if bad_e:  issues.append(f'«Fig.»/«Figure» вместо «Рисунок» ({len(bad_e)} шт.)')
    if bad_nr: issues.append(f'«Рисунок №N», знак № избыточен ({len(bad_nr)} шт.)')

    if issues:
        return Check('F6', 'Подписи рисунков', False,
                     'Неверный формат: ' + '; '.join(issues) +
                     '. Требуется «Рисунок 1 – Название» (ГОСТ 7.32 п.6.5.1)')
    if good:
        return Check('F6', 'Подписи рисунков', True,
                     f'Правильный формат. Подписей: {len(good)}')
    return Check('F6', 'Подписи рисунков', False,
                 'Подписи рисунков в формате «Рисунок N» не найдены '
                 '(ГОСТ 7.32 п.6.5.1)', 'warning')


def _figure_mentions(report: dict) -> Check:
    """п.6.5.1, На каждый рисунок должна быть ссылка в тексте."""
    full_text = report.get('full_text', '')
    captions: set = set()
    mentions: set = set()

    for line in full_text.split('\n'):
        stripped = line.strip()
        cap = re.match(r'^Рисунок\s+(\d+)', stripped)
        if cap:
            captions.add(int(cap.group(1)))
            continue
        for num in re.findall(r'(?:рисунк\w*|рис\.)\s*(\d+)', stripped, re.IGNORECASE):
            mentions.add(int(num))

    if not captions:
        return Check('F7', 'Ссылки на рисунки в тексте', True,
                     'Подписей к рисункам нет – проверять нечего')

    missing = sorted(captions - mentions)
    if not missing:
        return Check('F7', 'Ссылки на рисунки в тексте', True,
                     f'Ссылки есть на все рисунки ({len(captions)} шт.)')
    listed = ', '.join(str(n) for n in missing[:8])
    return Check('F7', 'Ссылки на рисунки в тексте', False,
                 f'Нет ссылок в тексте на рисунки: {listed}'
                 f'{" и другие" if len(missing) > 8 else ""} '
                 f'(ГОСТ 7.32 п.6.5.1)',
                 'warning' if len(missing) < len(captions) else 'error')


def _table_captions(report: dict) -> Check:
    """п.6.6, Заголовки таблиц «Таблица N»."""
    full_text = report.get('full_text', '')
    if not re.search(r'таблиц', full_text, re.IGNORECASE):
        return Check('F8', 'Подписи таблиц', True, 'Таблицы не обнаружены')

    good = re.findall(r'\bТаблица\s+\d+', full_text)
    bad  = re.findall(r'\bТабл?\.\s*\d+', full_text)

    if bad:
        return Check('F8', 'Подписи таблиц', False,
                     f'«Табл.» вместо «Таблица» ({len(bad)} шт.) (ГОСТ 7.32 п.6.6.1)')
    if good:
        return Check('F8', 'Подписи таблиц', True,
                     f'Правильный формат. Заголовков: {len(good)}')
    return Check('F8', 'Подписи таблиц', False,
                 'Заголовки таблиц не найдены', 'warning')


def _heading_no_dot(report: dict) -> Check:
    """п.6.2.3, Заголовки без точки в конце."""
    bad = []
    bad_keys = set()
    for rec in _chapter_lines(report):
        s = rec['line']
        if not s or not s.endswith('.'):
            continue
        numbered = bool(re.match(r'^\d+(?:\.\d+)*\.?\s+', s))
        uppercase = bool(re.fullmatch(
            r'[А-ЯЁA-Z][А-ЯЁA-Z \t]{4,}', s[:-1].strip()))
        structural = bool(_line_heading_matches(
            s, _STRUCTURAL_HEADING, allow_dot=True))
        if len(s) >= 120 or not (numbered or uppercase or structural):
            continue
        if rec['in_list'] or not rec['has_text_after']:
            continue
        bad.append(s[:70])
        bad_keys.add(rec['key'])

    # Contents and references pages are intentionally excluded from chapter
    # analysis, but their own structural headings still obey the no-dot rule.
    for page, text in _non_front_page_texts(report):
        lines = [(index, line.strip())
                 for index, line in enumerate(text.splitlines())
                 if line.strip() and not re.fullmatch(r'\d+', line.strip())]
        for position, (index, line) in enumerate(lines):
            key = (page, index)
            if key in bad_keys or not line.endswith('.'):
                continue
            if not _line_heading_matches(
                    line, _STRUCTURAL_HEADING, allow_dot=True):
                continue
            if not any(value for _, value in lines[position + 1:]):
                continue
            bad.append(line[:70])
            bad_keys.add(key)

    if bad:
        return Check('F9', 'Точки в конце заголовков', False,
                     f'{len(bad)} заголовков с точкой в конце: «{bad[0]}» '
                     f'(ГОСТ 7.32 п.6.2.3)')
    return Check('F9', 'Точки в конце заголовков', True)


def _cite_format(report: dict) -> Check:
    """п.5.10.2, Ссылки на источники в формате [N]."""
    full_text = report.get('full_text', '')
    has_refs_section = bool(re.search(
        r'СПИСОК\s+(?:ИСПОЛЬЗОВАННЫХ\s+|ИСПОЛЬЗУЕМЫХ\s+)?(?:ИСТОЧНИКОВ|ЛИТЕРАТУРЫ)'
        r'|ЛИТЕРАТУРА|БИБЛИОГРАФИЯ',
        full_text, re.IGNORECASE))

    if not has_refs_section:
        return Check('F10', 'Ссылки на источники [N]', False,
                     'Список источников отсутствует', 'warning')

    bracket = re.findall(r'\[\d+\]', full_text)
    paren   = re.findall(r'\(\d+\)', full_text)

    if bracket:
        return Check('F10', 'Ссылки на источники [N]', True,
                     f'Ссылки [N] в тексте: {len(bracket)} шт.')
    if paren:
        return Check('F10', 'Ссылки на источники [N]', False,
                     f'Ссылки оформлены как (N) вместо [N] ({len(paren)} шт.) '
                     f'(ГОСТ 7.32 п.5.10.2)', 'warning')
    return Check('F10', 'Ссылки на источники [N]', False,
                 'Ссылки на источники в тексте не найдены (ГОСТ 7.32 п.5.10.2)',
                 'warning')


def _margins(report: dict) -> Check:
    """п.6.1.1, Поля: лев.30 / пр.15 / верх.20 / ниж.20 мм.

    Измеряется постранично, титульный раздел пропускается: там текст обычно
    выровнен по центру, левое поле измеряется неверно, да и рамка у титульного
    листа с заданием своя – в Word это отдельный раздел со своими полями.
    """
    skip = {m['page'] for m in (report.get('pages') or []) if _is_front(m)}
    all_pages = report.get('margins_by_page') or []
    # Если после пропусков не осталось ничего, меряем по всем листам: лучше
    # учесть титульный, чем молча отменить проверку полей.
    pages = [p for p in all_pages if p.get('page') not in skip] or all_pages

    if not pages:
        return Check('F11', 'Поля страницы', False,
                     'Не удалось определить поля', 'warning')

    # A margin is the strip the text never enters, so each side is measured by
    # the page that comes CLOSEST to that edge – not by an average. Averaging
    # breaks on every normal document: a chapter ending after two lines leaves
    # 200 mm of white space at the bottom, which is not a 200 mm bottom margin.
    # `wide_matters` – можно ли считать нарушением слишком широкое поле.
    # Снизу нельзя: раздел, кончившийся в середине листа, оставляет пустоту
    # законно, и это не увеличенное нижнее поле.
    sides = [
        ('Лев.',  MARGIN_LEFT,   [p['x0'] / PT_PER_MM for p in pages], True, MARGIN_TOL),
        ('Пр.',   MARGIN_RIGHT,  [(p['page_w'] - p['x1']) / PT_PER_MM for p in pages], True, MARGIN_TOL),
        ('Верх.', MARGIN_TOP,    [p['top'] / PT_PER_MM for p in pages], True, MARGIN_TOL_VERT),
        ('Ниж.',  MARGIN_BOTTOM, [(p['page_h'] - p['bottom']) / PT_PER_MM for p in pages], False, MARGIN_TOL_VERT),
    ]

    measured, intrudes, indented = [], [], []
    for label, expected, values, wide_matters, tol in sides:
        closest = min(values)
        measured.append(f'{label} {closest:.0f}мм')
        if closest < expected - tol:
            worst = pages[values.index(closest)]['page']
            intrudes.append(f'{label} {closest:.0f}мм при норме {expected}мм '
                            f'(стр. {worst})')
        elif wide_matters and closest > expected + MARGIN_WIDE_TOL:
            indented.append(f'{label} {closest:.0f}мм при норме {expected}мм')

    summary = ' / '.join(measured)
    if intrudes:
        return Check('F11', 'Поля страницы', False,
                     'Текст заходит в поле: ' + ', '.join(intrudes) +
                     '. Требуется Лев.30/Пр.15/Верх.20/Ниж.20мм (ГОСТ 7.32 п.6.1.1)')
    if indented:
        return Check('F11', 'Поля страницы', False,
                     'Поля шире нормы, текст нигде не доходит до границы: ' +
                     ', '.join(indented) + ' (ГОСТ 7.32 п.6.1.1)', 'warning')
    return Check('F11', 'Поля страницы', True, summary)


def _uppercase_structural_headings(report: dict) -> Check:
    """п.6.2.2, structural headings are set in uppercase letters."""
    specs = [
        ('РЕФЕРАТ', True),
        (_CONTENTS_HEADING, False),
        ('ВВЕДЕНИЕ', True),
        ('ЗАКЛЮЧЕНИЕ', True),
        (_REFERENCES_HEADING, True),
    ]
    wrong = []
    for pattern, body in specs:
        found = _heading_text(report, pattern, body=body)
        if found and found != found.upper():
            expected = found.rstrip('.').upper()
            wrong.append(f'«{found}» (нужно «{expected}»)')

    if wrong:
        return Check(
            'F12', 'Прописные заголовки структурных элементов', False,
            'Разделы найдены, но заголовки набраны не прописными: '
            + ', '.join(wrong)
            + ' (ГОСТ 7.32 п.6.2.2)',
            'warning',
        )
    return Check('F12', 'Прописные заголовки структурных элементов', True)
