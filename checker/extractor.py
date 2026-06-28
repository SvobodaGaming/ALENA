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

            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ''
                result['text_by_page'].append(text)
                if i > 0:  # skip title page for margin/font analysis
                    all_body_chars.extend(page.chars or [])

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

            # Margin info: use percentile-based bounds to exclude outliers
            if all_body_chars:
                x0s = sorted(c['x0']
                             for c in all_body_chars if c.get('x0') is not None)
                x1s = sorted(c['x1']
                             for c in all_body_chars if c.get('x1') is not None)
                tops = sorted(c['top']
                              for c in all_body_chars if c.get('top') is not None)
                bots = sorted(c['bottom']
                              for c in all_body_chars if c.get('bottom') is not None)
                n = len(x0s)
                p5 = max(n // 20, 0)

                ref_page = pdf.pages[1] if len(pdf.pages) > 1 else pdf.pages[0]
                result['margin_info'] = {
                    'page_w': float(ref_page.width),
                    'page_h': float(ref_page.height),
                    'x0': x0s[p5] if x0s else None,
                    'x1': x1s[-(p5 + 1)] if x1s else None,
                    'top': tops[p5] if tops else None,
                    'bottom': bots[-(p5 + 1)] if bots else None,
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

    # Full name from title page
    name_found = False
    for pat in [
        r'(?:Выполнил[аи]?|студент[а]?)[^\n]{0,30}\n[^\n]{0,30}\n\s*([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)',
        r'(?:Выполнил[аи]?|студент[а]?)\s*[:\n]\s*([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)',
        r'([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)\s*\n[^\n]{0,30}(?:группы?|курс)',
    ]:
        m = re.search(pat, title_text, re.IGNORECASE | re.MULTILINE)
        if m and len(m.group(1).split()) >= 2:
            student['name'] = m.group(1).strip()
            name_found = True
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
