"""PDF content extraction: text, images, fonts, margins, student identity."""
import re
import io
from pathlib import Path

import pdfplumber
import fitz  # PyMuPDF
from PIL import Image

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


def _content_bounds(page):
    """Bounding box (x0, x1, top, bottom) of the page's text body, in points.

    Standalone short numbers near the top/bottom edge are page numbers: they
    live inside the required margin and must not shrink the measured one.
    Returns None when the page has no qualifying words.
    """
    try:
        words = page.extract_words() or []
    except Exception:
        return None
    h = float(page.height)
    content = [
        w for w in words
        if not (w['text'].strip().isdigit() and len(w['text'].strip()) <= 4
                and (w['top'] < 0.1 * h or w['bottom'] > 0.9 * h))
    ]
    if not content:
        return None
    return (min(w['x0'] for w in content),
            max(w['x1'] for w in content),
            min(w['top'] for w in content),
            max(w['bottom'] for w in content))


def extract_report(pdf_path: str) -> dict:
    result = {
        'path': pdf_path,
        'filename': Path(pdf_path).name,
        'pages_count': 0,
        'text_by_page': [],
        'full_text': '',
        'images': [],
        'font_info': {},
        'margin_info': {},
        'student': {},
        'is_scanned': False,
        'error': None,
    }

    try:
        with pdfplumber.open(pdf_path) as pdf:
            result['pages_count'] = len(pdf.pages)
            all_body_chars = []
            page_bounds = []

            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ''
                result['text_by_page'].append(text)
                if i > 0:  # skip title page for margin/font analysis
                    all_body_chars.extend(page.chars or [])
                    bounds = _content_bounds(page)
                    if bounds:
                        page_bounds.append(bounds)

            result['full_text'] = '\n'.join(result['text_by_page'])

            # Detect scanned PDF (no extractable text)
            avg_chars = len(result['full_text']) / \
                max(result['pages_count'], 1)
            result['is_scanned'] = avg_chars < 80

            # Font info: (fontname, size) -> character count
            font_counts: dict = {}
            for ch in all_body_chars:
                fname = ch.get('fontname', 'Unknown')
                fsize = round(float(ch.get('size', 0)))
                key = (fname, fsize)
                font_counts[key] = font_counts.get(key, 0) + 1
            result['font_info'] = font_counts

            # Margin info: per-page text-body bounding boxes (page numbers
            # excluded), then the median across pages so a single page with a
            # wide table or figure caption does not skew the result.
            if page_bounds:
                ref_page = pdf.pages[1] if len(pdf.pages) > 1 else pdf.pages[0]
                result['margin_info'] = {
                    'page_w': float(ref_page.width),
                    'page_h': float(ref_page.height),
                    'x0':     _median([b[0] for b in page_bounds]),
                    'x1':     _median([b[1] for b in page_bounds]),
                    'top':    _median([b[2] for b in page_bounds]),
                    'bottom': _median([b[3] for b in page_bounds]),
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
                        result['images'].append({
                            'page': page_num + 1,
                            'pil': pil_img,
                            'w': pil_img.width,
                            'h': pil_img.height,
                        })
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
