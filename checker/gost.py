"""GOST 7.32-2017 compliance checker for PDF reports."""
import re

PT_PER_MM = 2.834645669
# Required margins (mm) per GOST 7.32 п.6.1.1
MARGIN_LEFT   = 30
MARGIN_RIGHT  = 15
MARGIN_TOP    = 20
MARGIN_BOTTOM = 20
MARGIN_TOL    = 6  # mm tolerance


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


# Canonical list of all checks (code, name, group) — single source of truth for
# the UI checkboxes. Keep the codes/names in sync with the Check() instances
# created below. 'structure' = структурные элементы, 'format' = оформление.
GOST_CHECKS = [
    ('S1', 'Титульный лист',           'structure'),
    ('S2', 'Реферат',                  'structure'),
    ('S3', 'Содержание',               'structure'),
    ('S4', 'Введение / Цель',          'structure'),
    ('S5', 'Заключение',               'structure'),
    ('S6', 'Список источников',        'structure'),
    ('F1', 'Нумерация страниц',        'format'),
    ('F2', 'Нумерация разделов',       'format'),
    ('F3', 'Подписи рисунков',         'format'),
    ('F4', 'Подписи таблиц',           'format'),
    ('F5', 'Точки в конце заголовков', 'format'),
    ('F6', 'Цитирование [N]',          'format'),
    ('F7', 'Шрифт Times New Roman',    'format'),
    ('F8', 'Размер шрифта ≥12пт',      'format'),
    ('F9', 'Поля страницы',            'format'),
]

ALL_CODES = [c[0] for c in GOST_CHECKS]


def check_gost(report: dict, enabled=None) -> list:
    """Run all GOST checks, optionally keeping only a subset.

    enabled: iterable of check codes (e.g. {'S1', 'F7'}) to include. When None
    (the default) every check is returned, preserving prior behaviour.
    """
    text_by_page = report.get('text_by_page', [])
    full_text = report.get('full_text', '')
    font_info = report.get('font_info', {})
    margin_info = report.get('margin_info', {})
    pages = report.get('pages_count', 0)

    checks = [
        _title_page(text_by_page),
        _abstract(full_text),
        _toc(full_text),
        _introduction(full_text),
        _conclusion(full_text),
        _references(full_text),
        _page_numbers(text_by_page, pages),
        _section_numbering(full_text),
        _figure_captions(full_text),
        _table_captions(full_text),
        _heading_no_dot(full_text),
        _cite_format(full_text),
        _font_family(font_info),
        _font_size(font_info),
        _margins(margin_info),
    ]
    results = [c.to_dict() for c in checks]
    if enabled is not None:
        allow = set(enabled)
        results = [c for c in results if c['code'] in allow]
    return results


def _title_page(text_by_page: list) -> Check:
    """п.5.1, Титульный лист."""
    title_text = '\n'.join(text_by_page[:2]) if text_by_page else ''

    has_org = bool(re.search(
        r'(федеральн|министерств|университет|институт|академи|учреждени)',
        title_text, re.IGNORECASE))
    has_year = bool(re.search(r'\b20\d{2}\b', title_text))
    has_author = bool(re.search(
        r'(выполнил|студент|автор)', title_text, re.IGNORECASE))
    has_city = bool(re.search(
        r'(Москва|Санкт-Петербург|Екатеринбург|Новосибирск|Казань|Самара'
        r'|Алматы|Алма-Ата|Астана|Нур-Султан|Шымкент|Семей|Атырау|Актобе'
        r'|\bМ\.\b)',
        title_text))

    score = has_org + has_year + has_author + has_city
    missing = []
    if not has_org:    missing.append('наименование организации')
    if not has_author: missing.append('ФИО исполнителя')
    if not has_year:   missing.append('год составления')
    if not has_city:   missing.append('место составления')

    if score >= 3:
        return Check('S1', 'Титульный лист', True,
                     f'Найден (организация:{has_org} автор:{has_author} год:{has_year})')
    if score >= 2:
        return Check('S1', 'Титульный лист', False,
                     f'Неполный, отсутствует: {", ".join(missing)}', 'warning')
    return Check('S1', 'Титульный лист', False,
                 'Титульный лист не обнаружен (ГОСТ 7.32 п.5.1)')


def _abstract(full_text: str) -> Check:
    """п.5.3, Реферат."""
    if re.search(r'\bРЕФЕРАТ\b', full_text):
        return Check('S2', 'Реферат', True)
    return Check('S2', 'Реферат', False,
                 'Структурный элемент "РЕФЕРАТ" отсутствует (ГОСТ 7.32 п.5.3)', 'warning')


def _toc(full_text: str) -> Check:
    """п.5.4, Содержание."""
    if re.search(r'\b(СОДЕРЖАНИЕ|ОГЛАВЛЕНИЕ)\b', full_text):
        return Check('S3', 'Содержание', True)
    return Check('S3', 'Содержание', False,
                 'Структурный элемент "СОДЕРЖАНИЕ" отсутствует (ГОСТ 7.32 п.5.4)')


def _introduction(full_text: str) -> Check:
    """п.5.7, Введение."""
    if re.search(r'\b(ВВЕДЕНИЕ|ЦЕЛИ?\s+РАБОТЫ|ЦЕЛИ?\s*\n)', full_text):
        return Check('S4', 'Введение / Цель', True)
    return Check('S4', 'Введение / Цель', False,
                 'Структурный элемент "ВВЕДЕНИЕ" отсутствует (ГОСТ 7.32 п.5.7)')


def _conclusion(full_text: str) -> Check:
    """п.5.9, Заключение."""
    if re.search(r'\bЗАКЛЮЧЕНИЕ\b', full_text):
        return Check('S5', 'Заключение', True)
    return Check('S5', 'Заключение', False,
                 'Структурный элемент "ЗАКЛЮЧЕНИЕ" отсутствует (ГОСТ 7.32 п.5.9)')


def _references(full_text: str) -> Check:
    """п.5.10, Список использованных источников."""
    if re.search(
        r'\b(СПИСОК\s+(?:ИСПОЛЬЗОВАННЫХ\s+)?(?:ИСТОЧНИКОВ|ЛИТЕРАТУРЫ|ССЫЛОК)|БИБЛИОГРАФИЯ|ЛИТЕРАТУРА)\b',
        full_text, re.IGNORECASE
    ):
        return Check('S6', 'Список источников', True)
    return Check('S6', 'Список источников', False,
                 'Список использованных источников отсутствует (ГОСТ 7.32 п.5.10)', 'warning')


def _page_numbers(text_by_page: list, total_pages: int) -> Check:
    """п.6.3, Нумерация страниц арабскими цифрами."""
    if total_pages <= 1:
        return Check('F1', 'Нумерация страниц', True, 'Документ одностраничный')

    found = 0
    for i, text in enumerate(text_by_page[1:], 2):
        lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
        for line in (lines[:1] + lines[-1:]):
            if re.match(r'^\d+$', line) and 2 <= int(line) <= 999:
                found += 1
                break

    body_pages = total_pages - 1
    if body_pages == 0:
        return Check('F1', 'Нумерация страниц', True)
    ratio = found / body_pages

    if ratio >= 0.5:
        return Check('F1', 'Нумерация страниц', True,
                     f'Номера найдены на {found}/{body_pages} страницах')
    if ratio >= 0.2:
        return Check('F1', 'Нумерация страниц', False,
                     f'Нумерация нерегулярна: {found}/{body_pages} страниц', 'warning')
    return Check('F1', 'Нумерация страниц', False,
                 'Нумерация страниц не обнаружена (ГОСТ 7.32 п.6.3)')


def _section_numbering(full_text: str) -> Check:
    """п.6.4, Нумерация разделов без точки после номера."""
    # Correct: "1 TITLE", "1.1 Title"   Wrong: "1. TITLE", "1.1. Title"
    good = re.findall(r'^(\d+(?:\.\d+)*)\s+[А-ЯЁA-ZА-яёa-z]', full_text, re.MULTILINE)
    bad  = re.findall(r'^(\d+(?:\.\d+)*)\.[ \t]+[А-ЯЁA-Za-zА-яё]', full_text, re.MULTILINE)

    if not good and not bad:
        return Check('F2', 'Нумерация разделов', False,
                     'Нумерованные разделы не обнаружены', 'warning')
    if bad:
        return Check('F2', 'Нумерация разделов', False,
                     f'Точка после номера раздела ({bad[0]}.), не допускается '
                     f'(ГОСТ 7.32 п.6.4.1). Найдено: {len(bad)} случаев', 'warning')
    return Check('F2', 'Нумерация разделов', True,
                 f'Нумерованных разделов: {len(good)}')


def _figure_captions(full_text: str) -> Check:
    """п.6.5, Подписи рисунков должны быть «Рисунок N»."""
    has_fig_refs = bool(re.search(r'рисун|рис\.|fig\.?', full_text, re.IGNORECASE))
    if not has_fig_refs:
        return Check('F3', 'Подписи рисунков', True, 'Рисунки не обнаружены')

    good   = re.findall(r'\bРисунок\s+\d+', full_text)
    bad_r  = re.findall(r'\bРис\.\s*\d+', full_text)
    bad_e  = re.findall(r'\b(?:Fig\.|Figure)\s*\d+', full_text, re.IGNORECASE)
    bad_nr = re.findall(r'\bрисунок\s+№\s*\d+', full_text, re.IGNORECASE)  # лишний №

    issues = []
    if bad_r:  issues.append(f'"Рис." вместо "Рисунок" ({len(bad_r)} шт.)')
    if bad_e:  issues.append(f'"Fig."/"Figure" вместо "Рисунок" ({len(bad_e)} шт.)')
    if bad_nr: issues.append(f'"Рисунок №N", знак № избыточен ({len(bad_nr)} шт.)')

    if issues:
        return Check('F3', 'Подписи рисунков', False,
                     'Неверный формат: ' + '; '.join(issues) +
                     '. Требуется: "Рисунок N, Название" (ГОСТ 7.32 п.6.5.1)')
    if good:
        return Check('F3', 'Подписи рисунков', True,
                     f'Правильный формат. Подписей: {len(good)}')
    return Check('F3', 'Подписи рисунков', False,
                 'Подписи рисунков в формате "Рисунок N" не найдены (ГОСТ 7.32 п.6.5.1)',
                 'warning')


def _table_captions(full_text: str) -> Check:
    """п.6.6, Заголовки таблиц «Таблица N»."""
    has_tables = bool(re.search(r'таблиц', full_text, re.IGNORECASE))
    if not has_tables:
        return Check('F4', 'Подписи таблиц', True, 'Таблицы не обнаружены')

    good = re.findall(r'\bТаблица\s+\d+', full_text)
    bad  = re.findall(r'\bТабл?\.\s*\d+', full_text)

    if bad:
        return Check('F4', 'Подписи таблиц', False,
                     f'"Табл." вместо "Таблица" ({len(bad)} шт.) '
                     f'(ГОСТ 7.32 п.6.6.1)')
    if good:
        return Check('F4', 'Подписи таблиц', True,
                     f'Правильный формат. Заголовков: {len(good)}')
    return Check('F4', 'Подписи таблиц', False,
                 'Заголовки таблиц не найдены', 'warning')


def _heading_no_dot(full_text: str) -> Check:
    """п.6.2.3, Заголовки без точки в конце."""
    bad = []
    for line in full_text.split('\n'):
        s = line.strip()
        if not s or not s.endswith('.'):
            continue
        # Looks like a heading: numbered or ALL-CAPS, short, no trailing colon
        if len(s) < 120 and (
            re.match(r'^\d+[\d.]*\s+', s) or
            re.match(r'^[А-ЯЁ\s]{5,}$', s[:-1].strip())
        ):
            bad.append(s[:70])

    if bad:
        return Check('F5', 'Точки в конце заголовков', False,
                     f'{len(bad)} заголовков с точкой в конце: «{bad[0]}» '
                     f'(ГОСТ 7.32 п.6.2.3)')
    return Check('F5', 'Точки в конце заголовков', True)


def _cite_format(full_text: str) -> Check:
    """п.5.10.2, Ссылки на источники в формате [N]."""
    has_refs_section = bool(re.search(
        r'СПИСОК\s+(?:ИСПОЛЬЗОВАННЫХ\s+)?(?:ИСТОЧНИКОВ|ЛИТЕРАТУРЫ)|ЛИТЕРАТУРА',
        full_text, re.IGNORECASE))

    if not has_refs_section:
        return Check('F6', 'Цитирование [N]', False,
                     'Список источников отсутствует', 'warning')

    bracket = re.findall(r'\[\d+\]', full_text)
    paren   = re.findall(r'\(\d+\)', full_text)

    if bracket:
        return Check('F6', 'Цитирование [N]', True,
                     f'Ссылки [N] в тексте: {len(bracket)} шт.')
    if paren:
        return Check('F6', 'Цитирование [N]', False,
                     f'Ссылки оформлены как (N) вместо [N] ({len(paren)} шт.) '
                     f'(ГОСТ 7.32 п.5.10.2)', 'warning')
    return Check('F6', 'Цитирование [N]', False,
                 'Ссылки на источники в тексте не найдены (ГОСТ 7.32 п.5.10.2)', 'warning')


def _font_family(font_info: dict) -> Check:
    """п.6.1.1, Основной шрифт Times New Roman."""
    if not font_info:
        return Check('F7', 'Шрифт Times New Roman', False,
                     'Данные о шрифтах недоступны', 'warning')

    totals: dict = {}
    for (fname, _), count in font_info.items():
        key = fname.lower().replace('-', '').replace(' ', '')
        totals[key] = totals.get(key, 0) + count

    total = sum(totals.values())
    if total == 0:
        return Check('F7', 'Шрифт Times New Roman', False,
                     'Символов не найдено', 'warning')

    tnr = sum(v for k, v in totals.items() if 'times' in k or 'timesnewroman' in k)
    ratio = tnr / total

    if ratio >= 0.7:
        return Check('F7', 'Шрифт Times New Roman', True,
                     f'Times New Roman: {ratio:.0%} символов')
    if ratio >= 0.3:
        return Check('F7', 'Шрифт Times New Roman', False,
                     f'Times New Roman лишь в {ratio:.0%} символов '
                     f'(требуется основной шрифт TNR, ГОСТ 7.32 п.6.1.1)', 'warning')

    top_fonts = sorted(totals.items(), key=lambda x: -x[1])[:3]
    names = ', '.join(f[0][:25] for f in top_fonts)
    return Check('F7', 'Шрифт Times New Roman', False,
                 f'Times New Roman не обнаружен. Используется: {names}. '
                 f'Требуется TNR (ГОСТ 7.32 п.6.1.1)')


def _font_size(font_info: dict) -> Check:
    """п.6.1.1, Размер шрифта не менее 12 пт."""
    if not font_info:
        return Check('F8', 'Размер шрифта ≥12пт', False,
                     'Данные о размерах шрифта недоступны', 'warning')

    size_totals: dict = {}
    for (_, fsize), count in font_info.items():
        if fsize > 0:
            size_totals[fsize] = size_totals.get(fsize, 0) + count

    if not size_totals:
        return Check('F8', 'Размер шрифта ≥12пт', False,
                     'Размеры шрифта не определены', 'warning')

    total = sum(size_totals.values())
    small = sum(v for k, v in size_totals.items() if k < 12)
    dominant = max(size_totals, key=size_totals.get)

    if small / total > 0.35:
        return Check('F8', 'Размер шрифта ≥12пт', False,
                     f'{small/total:.0%} текста имеет размер <12пт. '
                     f'Доминирующий: {dominant}пт (ГОСТ 7.32 п.6.1.1)')
    if dominant < 12:
        return Check('F8', 'Размер шрифта ≥12пт', False,
                     f'Основной размер {dominant}пт < 12пт (ГОСТ 7.32 п.6.1.1)')
    return Check('F8', 'Размер шрифта ≥12пт', True,
                 f'Доминирующий размер: {dominant}пт')


def _margins(margin_info: dict) -> Check:
    """п.6.1.1, Поля: лев.30/пр.15/верх.20/ниж.20 мм."""
    if not margin_info or margin_info.get('x0') is None:
        return Check('F9', 'Поля страницы', False,
                     'Не удалось определить поля', 'warning')

    pw = margin_info.get('page_w', 595.28)
    ph = margin_info.get('page_h', 841.89)
    x0 = margin_info['x0']
    x1 = margin_info.get('x1')
    top = margin_info.get('top')
    bot = margin_info.get('bottom')

    left_mm   = x0 / PT_PER_MM
    right_mm  = (pw - x1) / PT_PER_MM if x1 else None
    top_mm    = top / PT_PER_MM if top else None
    bottom_mm = (ph - bot) / PT_PER_MM if bot else None

    issues = []
    measured = []

    def chk(actual, expected, label):
        if actual is None:
            return
        measured.append(f'{label}:{actual:.0f}мм')
        if abs(actual - expected) > MARGIN_TOL:
            issues.append(f'{label} {actual:.0f}мм (норма {expected}мм)')

    chk(left_mm,   MARGIN_LEFT,   'Лев.')
    chk(right_mm,  MARGIN_RIGHT,  'Пр.')
    chk(top_mm,    MARGIN_TOP,    'Верх.')
    chk(bottom_mm, MARGIN_BOTTOM, 'Ниж.')

    if issues:
        return Check('F9', 'Поля страницы', False,
                     'Несоответствие: ' + ', '.join(issues) +
                     '. Требуется Лев.30/Пр.15/Верх.20/Ниж.20мм (ГОСТ 7.32 п.6.1.1)',
                     'warning')
    return Check('F9', 'Поля страницы', True, ' / '.join(measured))
