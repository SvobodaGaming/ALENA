"""PDF content extraction: text, images, fonts, margins, student identity."""
import re
import io
from pathlib import Path

import pdfplumber
import fitz  # PyMuPDF
from PIL import Image

from checker.image_plagiarism import image_meta

# Points per mm
PT_PER_MM = 2.834645669
A4_W_PT = 595.28
A4_H_PT = 841.89


def _median(values: list) -> float:
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


CAPTION_RE = re.compile(r'^\s*(рисунок|рис\.|таблица|табл\.)\s*\d', re.IGNORECASE)
# Заголовок листа задания: «ЗАДАНИЕ», «Индивидуальное задание на практику».
# После «задание» стоит граница слова — «заданием», «задания» в обычной фразе
# заголовком не считаются.
TASK_HEAD_RE = re.compile(
    r'^(?:индивидуальное|календарное|примерное|тематическое)?\s*задание\b',
    re.IGNORECASE)
TASK_KIND_RE = re.compile(r'(практик|курсов|выпускн|дипломн)', re.IGNORECASE)
TASK_HEAD_LINES = 20      # заголовок листа стоит вверху, под шапкой вуза
TASK_HEAD_MAX = 60        # строка заголовка короче строки основного текста


def _is_task_page(text: str) -> bool:
    """A «задание на практику / на курсовую работу» sheet. Together with the
    title page it is exempt from the 14 pt rule — only the typeface counts.

    Слово ищется в заголовке — отдельной короткой строкой вверху листа. Раньше
    хватало любого «задание» среди первых четырёхсот знаков, и обычная фраза
    «в соответствии с индивидуальным заданием на практику» превращала лист в
    задание: с него не брались ни поля, ни размер шрифта, ни номер страницы.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:TASK_HEAD_LINES]
    if not lines or not TASK_KIND_RE.search('\n'.join(lines)):
        return False
    return any(TASK_HEAD_RE.match(ln) and not ln.endswith('.')
               and (len(ln) <= TASK_HEAD_MAX or ln.isupper())
               for ln in lines)


def _table_bboxes(page) -> list:
    """Bounding boxes of tables on the page. Skipped when the page has no
    ruling lines at all — find_tables() is the expensive part of extraction."""
    if not (page.lines or page.rects):
        return []
    try:
        return [t.bbox for t in page.find_tables()]
    except Exception:
        return []


def _classify_fonts(page) -> dict:
    """Split the page's characters into three groups and count (font, size).

    body    — running text, the part GOST holds to exactly 14 pt
    aux     — figure captions and text inside tables, allowed to be smaller
    pagenum — the page number in the top/bottom margin, typeface still matters
    """
    buckets = {'body': {}, 'aux': {}, 'pagenum': {}}
    h = float(page.height)
    tables = _table_bboxes(page)

    try:
        lines = page.extract_text_lines()
    except Exception:
        lines = []

    if not lines:                      # no line data: treat everything as body
        for ch in (page.chars or []):
            key = (ch.get('fontname', 'Unknown'), round(float(ch.get('size', 0)), 1))
            buckets['body'][key] = buckets['body'].get(key, 0) + 1
        return buckets

    for ln in lines:
        text = (ln.get('text') or '').strip()
        top, bottom = float(ln['top']), float(ln['bottom'])
        in_margin_band = top < 0.1 * h or bottom > 0.9 * h

        if text.isdigit() and len(text) <= 4 and in_margin_band:
            group = 'pagenum'
        elif CAPTION_RE.match(text) or any(
                bb[0] - 1 <= float(ln['x0']) and float(ln['x1']) <= bb[2] + 1
                and bb[1] - 1 <= top and bottom <= bb[3] + 1 for bb in tables):
            group = 'aux'
        else:
            group = 'body'

        for ch in ln.get('chars', []):
            key = (ch.get('fontname', 'Unknown'), round(float(ch.get('size', 0)), 1))
            buckets[group][key] = buckets[group].get(key, 0) + 1

    return buckets


def _merge_counts(target: dict, source: dict) -> None:
    for key, n in source.items():
        target[key] = target.get(key, 0) + n


HEAD_BAND_MM = 20      # колонтитул целиком лежит внутри обязательного поля
HEAD_MAX_CHARS = 60    # и занимает строку короче строки основного текста


def _text_lines(words: list) -> list:
    """Слова, собранные в строки: их вершины совпадают с точностью до пункта."""
    lines = []
    for w in sorted(words, key=lambda w: (w['top'], w['x0'])):
        if not w['text'].strip():
            continue
        if lines and w['top'] - lines[-1]['top'] <= 3:
            ln = lines[-1]
            ln['bottom'] = max(ln['bottom'], w['bottom'])
            ln['x0'] = min(ln['x0'], w['x0'])
            ln['x1'] = max(ln['x1'], w['x1'])
            ln['text'] += ' ' + w['text']
        else:
            lines.append({'top': w['top'], 'bottom': w['bottom'],
                          'x0': w['x0'], 'x1': w['x1'], 'text': w['text']})
    return lines


def _content_bounds(page):
    """Bounding box (x0, x1, top, bottom) of the page's text body, in points.

    Колонтитулы в измерении не участвуют: номер страницы, «- 5 -», «Отчёт по
    практике» стоят внутри обязательного поля по замыслу, и отчёт с полями
    точно по ГОСТу объявлялся из-за них нарушением. Колонтитул — короткая
    строка, целиком лежащая в полосе поля; строка основного текста туда
    целиком не помещается, поэтому съехавшие поля по-прежнему видны.

    Returns None when the page has no qualifying words.
    """
    try:
        words = page.extract_words() or []
    except Exception:
        return None
    h = float(page.height)
    band = HEAD_BAND_MM * PT_PER_MM
    content = []
    for ln in _text_lines(words):
        short = len(ln['text']) <= HEAD_MAX_CHARS
        if short and (ln['bottom'] <= band or ln['top'] >= h - band):
            continue
        # Отдельный номер страницы бывает и дальше от края — например, когда
        # поле задано с запасом. Такое число полем тоже не считается.
        if (short and ln['text'].strip().isdigit() and len(ln['text']) <= 4
                and (ln['top'] < 0.1 * h or ln['bottom'] > 0.9 * h)):
            continue
        content.append(ln)
    if not content:
        return None
    return (min(ln['x0'] for ln in content),
            max(ln['x1'] for ln in content),
            min(ln['top'] for ln in content),
            max(ln['bottom'] for ln in content))


def extract_report(pdf_path: str) -> dict:
    result = {
        'path': pdf_path,
        'filename': Path(pdf_path).name,
        'pages_count': 0,
        'text_by_page': [],
        'full_text': '',
        'images': [],
        'pages': [],
        'font_info': {},
        'margin_info': {},
        'margins_by_page': [],
        'student': {},
        'is_scanned': False,
        'error': None,
    }

    try:
        with pdfplumber.open(pdf_path) as pdf:
            result['pages_count'] = len(pdf.pages)
            pages_meta = []

            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ''
                result['text_by_page'].append(text)

                meta = {
                    'page':     i + 1,
                    'is_title': i == 0,
                    'is_task':  _is_task_page(text),
                    'fonts':    _classify_fonts(page),
                    'margins':  None,
                }
                bounds = _content_bounds(page)
                if bounds:
                    meta['margins'] = {
                        'page_w': float(page.width),
                        'page_h': float(page.height),
                        'x0': bounds[0], 'x1': bounds[1],
                        'top': bounds[2], 'bottom': bounds[3],
                    }
                pages_meta.append(meta)

            # Титульный лист и задание не участвуют в статистике размера
            # шрифта и в измерении полей. Если особыми вышли все листы до
            # единого, разметка ошиблась — проверять было бы нечего, и три
            # критерия разом отвалились бы с «определить не удалось».
            if len(pages_meta) > 2 and all(m['is_title'] or m['is_task']
                                           for m in pages_meta):
                for m in pages_meta[1:]:
                    m['is_task'] = False

            result['pages'] = pages_meta
            result['full_text'] = '\n'.join(result['text_by_page'])

            # Detect scanned PDF (no extractable text)
            avg_chars = len(result['full_text']) / \
                max(result['pages_count'], 1)
            result['is_scanned'] = avg_chars < 80

            # Font counts by role. Body text is taken from ordinary pages only:
            # the title page and the «задание» sheet are checked for typeface
            # but never for size, so their characters must not pollute the
            # 14 pt statistics.
            fonts_all, fonts_body, fonts_aux, fonts_pagenum = {}, {}, {}, {}
            fonts_special = {}
            for meta in pages_meta:
                special = meta['is_title'] or meta['is_task']
                for group, counts in meta['fonts'].items():
                    _merge_counts(fonts_all, counts)
                    if special:
                        _merge_counts(fonts_special, counts)
                    elif group == 'body':
                        _merge_counts(fonts_body, counts)
                    elif group == 'aux':
                        _merge_counts(fonts_aux, counts)
                if not special:
                    _merge_counts(fonts_pagenum, meta['fonts']['pagenum'])

            result['font_info'] = {
                'all':     fonts_all,
                'body':    fonts_body,
                'aux':     fonts_aux,
                'pagenum': fonts_pagenum,
                'special': fonts_special,
            }

            # Margins: measured on every page that has body text (the title
            # page included — its frame must comply too), reported per page so
            # the check can name the offending ones.
            page_bounds = [m['margins'] for m in pages_meta[1:] if m['margins']]
            result['margins_by_page'] = [
                dict(m['margins'], page=m['page'])
                for m in pages_meta if m['margins']
            ]
            if page_bounds:
                result['margin_info'] = {
                    'page_w': _median([b['page_w'] for b in page_bounds]),
                    'page_h': _median([b['page_h'] for b in page_bounds]),
                    'x0':     _median([b['x0'] for b in page_bounds]),
                    'x1':     _median([b['x1'] for b in page_bounds]),
                    'top':    _median([b['top'] for b in page_bounds]),
                    'bottom': _median([b['bottom'] for b in page_bounds]),
                }

    except Exception as e:
        result['error'] = str(e)
        return result

    # Extract images via PyMuPDF
    try:
        doc = fitz.open(pdf_path)
        seen_xrefs: set = set()
        for page_num in range(len(doc)):
            page = doc[page_num]
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    base_image = doc.extract_image(xref)
                    pil_img = Image.open(io.BytesIO(
                        base_image['image'])).convert('RGB')
                    if pil_img.width >= 50 and pil_img.height >= 50:
                        # Hashes/thumbnail now, full image dropped right away:
                        # keeping thousands of decoded PILs for the whole job
                        # is what used to OOM the host on large batches.
                        result['images'].append({
                            'page': page_num + 1,
                            **image_meta(pil_img),
                        })
                    pil_img.close()
                except Exception:
                    pass
        doc.close()
    except Exception:
        pass

    result['student'] = _identify_student(result['text_by_page'], pdf_path)
    return result


def _identify_student(text_by_page: list, pdf_path: str) -> dict:
    student = {'name': '', 'group': '',
               'work_title': '', 'year': '', 'org': ''}
    title_text = '\n'.join(text_by_page[:2]) if text_by_page else ''

    # Organisation
    for pat in [
        r'(ФЕДЕРАЛЬН\w+[^\n]+(?:УЧРЕЖДЕНИ\w+|УНИВЕРСИТЕТ)[^\n]+)',
        r'(МИНИСТЕРСТВ\w+[^\n]+)',
        r'((?:РОССИЙСКИЙ|ГОСУДАРСТВЕННЫЙ)[^\n]+УНИВЕРСИТЕТ[^\n]+)',
        r'((?:УНИВЕРСИТЕТ|АКАДЕМИЯ|ИНСТИТУТ)\s+[А-ЯЁ][^\n]{3,60})',
        r'([^\n]{3,60}(?:УНИВЕРСИТЕТ|АКАДЕМИЯ|ИНСТИТУТ)[^\n]{0,60})',
    ]:
        m = re.search(pat, title_text, re.IGNORECASE)
        if m:
            student['org'] = m.group(1).strip()[:120]
            break

    # Full name from title page.
    # Case-sensitive name classes (keywords are (?i:...)-insensitive locally):
    # a global IGNORECASE would make [А-ЯЁ][а-яё]+ match any word.
    NAME3   = r'[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2}'    # Фамилия Имя [Отчество]
    NAME_UP = r'[А-ЯЁ]{2,}(?:\s+[А-ЯЁ]{2,}){1,2}'          # ФАМИЛИЯ ИМЯ [ОТЧЕСТВО]
    FAM_IO  = r'[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*(?:[А-ЯЁ]\.)?'  # Фамилия И.[О.]
    IO_FAM  = r'[А-ЯЁ]\.\s*(?:[А-ЯЁ]\.)?\s*[А-ЯЁ][а-яё]+'  # И.[О.] Фамилия
    DOER    = r'(?i:выполнил[аи]?|подготовил[аи]?|разработал[аи]?|студент(?:ка)?|автор)'

    _STOP = {
        'работа', 'работы', 'работу', 'отчет', 'отчёт', 'группа', 'группы',
        'курс', 'курса', 'факультет', 'кафедра', 'дисциплина', 'дисциплине',
        'вариант', 'проверил', 'проверила', 'преподаватель', 'руководитель',
        'доцент', 'профессор', 'лабораторная', 'практическая', 'курсовая',
        'института', 'университета', 'направление', 'специальность',
    }

    def _plausible(cand: str) -> bool:
        return not any(w.lower().strip('.') in _STOP for w in cand.split())

    name_found = False
    for pat in [
        # «Выполнил: Иванов И.И.», «Студент группы АБ-21-04 Иванов Иван Иванович»
        rf'{DOER}[^\n]{{0,40}}?[:\s]\s*({FAM_IO}|{IO_FAM}|{NAME3}|{NAME_UP})\s*$',
        # ФИО на одной-двух строках ниже ключевого слова
        rf'{DOER}[^\n]{{0,40}}\n(?:[^\n]{{0,40}}\n)?\s*({FAM_IO}|{IO_FAM}|{NAME3}|{NAME_UP})\s*$',
        # ФИО строкой выше упоминания группы/курса
        rf'({NAME3})\s*,?\s*\n[^\n]{{0,30}}(?i:групп[аы]?|курс)',
        # ФИО сразу после номера группы
        rf'(?i:групп[аы]?)\s+[А-ЯЁA-Za-z]{{1,5}}[-–]\d{{2}}[-–]\d{{2,3}}[,\s]*\n?\s*({NAME3}|{FAM_IO})',
    ]:
        for m in re.finditer(pat, title_text, re.MULTILINE):
            cand = re.sub(r'\s+', ' ', m.group(1).strip())
            if not _plausible(cand):
                continue
            if cand.isupper():
                cand = ' '.join(w.capitalize() for w in cand.split())
            student['name'] = cand
            name_found = True
            break
        if name_found:
            break

    # Fallback: extract name from filename
    # Pattern: "ФАМИЛИЯ ИМЯ ОТЧЕСТВО_ID_assignsubmission_file_..."
    if not name_found:
        stem = Path(pdf_path).stem
        m = re.match(r'^([А-ЯЁ]+(?:\s+[А-ЯЁ]+){1,2})\s*_', stem)
        if m:
            parts = m.group(1).strip().split()
            student['name'] = ' '.join(p.capitalize() for p in parts)

    # Group
    for pat in [
        r'(?:группы?\s+)([А-ЯЁA-Za-z]{1,5}[-–]\d{2}[-–]\d{2,3})',
        r'\b([А-ЯЁA-Za-z]{1,5}[-–]\d{2}[-–]\d{2,3})\b',
    ]:
        m = re.search(pat, title_text)
        if m:
            student['group'] = m.group(1).strip()
            break

    # Fallback group from filename
    if not student['group']:
        stem = Path(pdf_path).stem
        m = re.search(r'([А-ЯЁA-Za-z]{1,5}[-_]\d{2}[-_]\d{2,3})', stem)
        if m:
            student['group'] = m.group(1).replace('_', '-')

    # Year
    m = re.search(r'\b(20\d{2})\b', title_text)
    if m:
        student['year'] = m.group(1)

    # Work title
    for pat in [
        r'(Практика\s*[№#]?\s*\d+[^\n]*)',
        r'(Лабораторная\s+работа\s*[№#]?\s*\d+[^\n]*)',
        r'(Курсовая\s+работа[^\n]*)',
    ]:
        m = re.search(pat, title_text, re.IGNORECASE)
        if m:
            student['work_title'] = m.group(1).strip()[:100]
            break

    return student
