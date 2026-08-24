"""PDF content extraction: text, images, fonts, margins, student identity.

Единственная точка разбора работы. DOCX, ODT и DOC доходят сюда уже
приведёнными к PDF (`checker/convert.py`), поэтому формат исходника ни на один
критерий не влияет – известно только его имя, и приходит оно параметром
`filename`.
"""
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
# После «задание» стоит граница слова – «заданием», «задания» в обычной фразе
# заголовком не считаются.
TASK_HEAD_RE = re.compile(
    r'^(?:индивидуальное|календарное|примерное|тематическое)?\s*задание\b',
    re.IGNORECASE)
TASK_KIND_RE = re.compile(r'(практик|курсов|выпускн|дипломн)', re.IGNORECASE)
TASK_HEAD_LINES = 20      # заголовок листа стоит вверху, под шапкой вуза
TASK_HEAD_MAX = 60        # строка заголовка короче строки основного текста


# Заголовок структурного элемента, с которого начинается собственно работа.
# Титульный раздел кончается на нём: всё, что до, – титул, задание и прочие
# бланки, свёрстанные по своей рамке.
FRONT_END_RE = re.compile(
    r'^[^\S\r\n]*(?:РЕФЕРАТ|СОДЕРЖАНИЕ|ОГЛАВЛЕНИЕ|ВВЕДЕНИЕ|АННОТАЦИЯ)'
    r'\.?[^\S\r\n]*$', re.MULTILINE | re.IGNORECASE)
# Титульный раздел длиннее этого не бывает: титул, задание на один-два листа,
# календарный план. Если структурный заголовок нашёлся только на седьмом листе,
# значит опознан он неверно, и молча снимать проверки с шести листов нельзя.
FRONT_MAX_PAGES = 5


def _is_task_page(text: str) -> bool:
    """A «задание на практику / на курсовую работу» sheet. Together with the
    title page it is exempt from the 14 pt rule – only the typeface counts.

    Слово ищется в заголовке – отдельной короткой строкой вверху листа. Раньше
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


def _front_matter_pages(pages_meta: list, texts: list) -> set:
    """Номера листов титульного раздела, у которых своей приметы нет.

    Задание редко умещается на один лист: перечень работ, календарный план,
    сроки и подписи переезжают на следующий, а заголовка «ЗАДАНИЕ» там уже нет.
    Свёрстан такой лист по-прежнему по рамке титульного раздела – в Word у неё
    свои поля, обычно уже нормы, – и, меряя его наравне с текстом, проверка
    ловила чужую рамку и объявляла нарушением работу, у которой с полями всё в
    порядке.

    Конвертация это обостряет: LibreOffice верстает чуть плотнее Word, и лист
    задания, влезавший в один, переносится на второй именно после неё. Одна и
    та же работа получала разные вердикты в DOCX и в PDF.

    Продолжением считаются листы после последнего опознанного и до первого
    листа со структурным заголовком – границы титульного раздела. Нет
    структурного заголовка или стоит он слишком далеко – ничего не снимаем:
    лучше лишняя придирка на одном листе, чем отменённая проверка на шести.
    """
    first_body = next((i for i, text in enumerate(texts)
                       if FRONT_END_RE.search(text or '')), None)
    if first_body is None or first_body > FRONT_MAX_PAGES:
        return set()
    anchor = max((i for i, m in enumerate(pages_meta[:first_body])
                  if m['is_title'] or m['is_task']), default=None)
    if anchor is None:
        return set()
    return {pages_meta[i]['page'] for i in range(anchor + 1, first_body)}


def _table_bboxes(page) -> list:
    """Bounding boxes of tables on the page. Skipped when the page has no
    ruling lines at all – find_tables() is the expensive part of extraction."""
    if not (page.lines or page.rects):
        return []
    try:
        return [t.bbox for t in page.find_tables()]
    except Exception:
        return []


def _classify_fonts(page) -> dict:
    """Split the page's characters into three groups and count (font, size).

    body    – running text, the part GOST holds to exactly 14 pt
    aux     – figure captions and text inside tables, allowed to be smaller
    pagenum – the page number in the top/bottom margin, typeface still matters
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
# Строка оглавления: заголовок, отточие и номер страницы у правого края.
LEADER_RE = re.compile(r'\.{4,}\s*\d{0,4}\s*$')


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
    точно по ГОСТу объявлялся из-за них нарушением. Колонтитул – короткая
    строка, целиком лежащая в полосе поля; строка основного текста туда
    целиком не помещается, поэтому съехавшие поля по-прежнему видны.

    Правый край меряется без строк оглавления. Номер страницы стоит в них по
    таб-стопу, и стоит он там, куда его поставил редактор: Word прижимает
    таб-стоп, заданный за границей набора, к самой границе, а LibreOffice
    выносит номер за неё – ровно настолько, насколько таб-стоп просрочен.
    Отточие тянется следом, и работа, у которой в Word номера стоят по краю
    поля, после конвертации из DOCX объявлялась вылезшей в поле. Замер по
    остальным строкам листа от этого расхождения не зависит.

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
        # Отдельный номер страницы бывает и дальше от края – например, когда
        # поле задано с запасом. Такое число полем тоже не считается.
        if (short and ln['text'].strip().isdigit() and len(ln['text']) <= 4
                and (ln['top'] < 0.1 * h or ln['bottom'] > 0.9 * h)):
            continue
        content.append(ln)
    if not content:
        return None
    # Если отточия на листе – единственный текст, мерить больше нечем.
    right = [ln for ln in content if not LEADER_RE.search(ln['text'])] or content
    return (min(ln['x0'] for ln in content),
            max(ln['x1'] for ln in right),
            min(ln['top'] for ln in content),
            max(ln['bottom'] for ln in content))


def extract_report(pdf_path: str, filename: str = '', error: str = '') -> dict:
    """Разобрать PDF и собрать по нему report – словарь, с которым дальше
    работают все проверки.

    filename – имя, под которым работу загрузил преподаватель. Для DOCX и ODT
    оно отличается от имени файла на диске: разбирается результат конвертации,
    а в отчёте и в ведомости должно стоять «Иванов.docx». Отсюда же берутся
    ФИО и группа, поэтому имя исходника важно и после конвертации.

    error – работу прочитать не удалось (не открылась, не конвертировалась).
    Разбор пропускается, но карточка в отчёте остаётся: ФИО из имени файла
    известно, и преподаватель видит, чья именно работа не прошла, вместо того
    чтобы недосчитаться её в ведомости молча.
    """
    result = {
        'path': pdf_path,
        'filename': filename or Path(pdf_path).name,
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

    if error:
        result['error'] = error
        result['student'] = _identify_student([], result['filename'])
        return result

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
                    'is_front': False,
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

            # Листы задания, оставшиеся без заголовка после переноса.
            # Отдельным признаком, а не is_task: лист задания в отчёте
            # называется по первому опознанному, и продолжение не должно
            # выдавать себя за него.
            front = _front_matter_pages(pages_meta, result['text_by_page'])
            for m in pages_meta:
                if m['page'] in front:
                    m['is_front'] = True

            # Титульный лист и задание не участвуют в статистике размера
            # шрифта и в измерении полей. Если особыми вышли все листы до
            # единого, разметка ошиблась – проверять было бы нечего, и три
            # критерия разом отвалились бы с «определить не удалось».
            if len(pages_meta) > 2 and all(m['is_title'] or m['is_task']
                                           or m['is_front'] for m in pages_meta):
                for m in pages_meta[1:]:
                    m['is_task'] = False
                    m['is_front'] = False

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
                special = (meta['is_title'] or meta['is_task']
                           or meta['is_front'])
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
            # page included – its frame must comply too), reported per page so
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
        # ФИО определяем и по нечитаемой работе: в ведомости она стоит строкой
        # «Иванов Иван – ошибка чтения», а не безымянным файлом.
        result['error'] = str(e)
        result['student'] = _identify_student(result['text_by_page'],
                                              result['filename'])
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

    result['student'] = _identify_student(result['text_by_page'],
                                          result['filename'])
    return result


# ─────────────────────────────── ФИО ────────────────────────────────────────
#
# Имя файла идёт первым, титульный лист – вторым. Выгрузка Moodle называет
# работы строго: «ФАМИЛИЯ ИМЯ ОТЧЕСТВО_<номер>_assignsubmission_file_<как файл
# назвал студент>». Титульный лист свёрстан у каждого по-своему – ФИО стоит то
# в таблице, то через пустую строку от «Выполнил», то в две колонки, – и
# разбор шапки вуза регулярно выдавал за студента «Минобрнауки России».

# Служебная связка Moodle: ФИО – всё, что стоит до неё. Номер отправки иногда
# отсутствует, тип бывает assignsubmission/assignfeedback/onlinetext.
MOODLE_NAME_RE = re.compile(
    r'^(.{3,120}?)_(?:\d+_)?(?:assign(?:submission|feedback)|onlinetext)_',
    re.IGNORECASE)
# Имя без связки: фамилия и одно-два слова или инициала, дальше разделитель.
_NAME_TOKEN = r'(?:[А-ЯЁ][А-ЯЁа-яё\-]+|(?:[А-ЯЁ]\.){1,2})'
PLAIN_NAME_RE = re.compile(
    rf'^([А-ЯЁ][А-ЯЁа-яё\-]+(?:\s+{_NAME_TOKEN}){{1,2}})\s*(?:[_.]|$)')

# Слово имени: «Иванов», «Иванова-Петрова», «Ivanov». Инициалы – «И.», «И.О.».
_NAME_WORD_RE = re.compile(
    r'^(?:[А-ЯЁ][а-яё]+|[A-Z][a-z]+)(?:-(?:[А-ЯЁ][а-яё]+|[A-Z][a-z]+))*$')
_INITIALS_RE = re.compile(r'^(?:[А-ЯЁA-Z]\.){1,2}$')
# Приставки составных имён: «Гасан оглы», «Айгуль кызы».
_NAME_PARTICLES = {'оглы', 'улы', 'уулу', 'кызы', 'гызы', 'ибн'}
# Отчество. Признак нужен там, где имя опознаётся без служебной связки Moodle.
_PATRONYMIC_RE = re.compile(r'(?:ович|евич|ьич|овна|евна|ична)$', re.IGNORECASE)

# Слова, которых в ФИО студента не бывает. Шапка титульного листа и подписи
# разделов – вот откуда брались ложные «фамилии».
_NOT_A_PERSON = {
    # ведомство и вуз
    'минобрнауки', 'минобразования', 'миннауки', 'минпросвещения',
    'министерство', 'министерства', 'россии', 'российской', 'российская',
    'федерации', 'федерация', 'рф', 'федеральное', 'федерального',
    'государственное', 'государственного', 'бюджетное', 'бюджетного',
    'автономное', 'образовательное', 'образовательного', 'учреждение',
    'учреждения', 'высшего', 'профессионального', 'образования',
    'университет', 'университета', 'институт', 'института', 'академия',
    'академии', 'филиал', 'филиала', 'колледж', 'техникум',
    # разделы титульного листа
    'работа', 'работы', 'работу', 'отчет', 'отчёт', 'отчета', 'отчёта',
    'группа', 'группы', 'курс', 'курса', 'факультет', 'факультета',
    'кафедра', 'кафедры', 'дисциплина', 'дисциплине', 'дисциплины',
    'вариант', 'варианта', 'тема', 'теме', 'темы', 'задание', 'задания',
    'проверил', 'проверила', 'выполнил', 'выполнила', 'принял', 'приняла',
    'преподаватель', 'руководитель', 'доцент', 'профессор', 'ассистент',
    'студент', 'студентка', 'студента', 'обучающийся', 'автор',
    'лабораторная', 'практическая', 'курсовая', 'практика', 'практике',
    'направление', 'специальность', 'москва', 'год', 'года', 'титульный',
}


def _clean_name(raw: str) -> str:
    """Приводит кусок имени файла или строки к виду «Слово Слово Слово».

    Точку справа не трогаем: ею кончаются инициалы, и «Иванов И.И.» после
    обрезки переставало быть похожим на имя вообще.
    """
    return re.sub(r'[\s_]+', ' ', raw).lstrip(' -.,').rstrip(' -,')


def _titlecase(name: str) -> str:
    """«АРТАМОНОВА ОЛЬГА» → «Артамонова Ольга». Инициалы остаются заглавными."""
    out = []
    for w in name.split():
        if _INITIALS_RE.match(w.upper()):
            out.append(w.upper())
        elif w.lower() in _NAME_PARTICLES:
            out.append(w.lower())
        else:
            out.append('-'.join(p.capitalize() for p in w.split('-')))
    return ' '.join(out)


def _looks_like_person(name: str, max_words: int = 3) -> bool:
    """Похоже ли на ФИО живого человека, а не на строку титульного листа.

    Считаем именем два-три слова, каждое из которых – слово с заглавной буквы
    или инициал, и ни одно не входит в словарь служебных слов. Хотя бы одно
    слово должно быть полным: «И. И.» без фамилии именем не считается.
    """
    parts = name.split()
    if not 2 <= len(parts) <= max_words:
        return False
    full_words = 0
    for p in parts:
        if p.lower().strip('.') in _NOT_A_PERSON:
            return False
        if _NAME_WORD_RE.match(p):
            full_words += 1
        elif p.lower() in _NAME_PARTICLES:
            continue
        elif not _INITIALS_RE.match(p):
            return False
    return full_words >= 1


def _name_from_filename(filename: str) -> str:
    """ФИО из имени файла – основной путь.

    Связка «_assignsubmission_» в собственных именах студенческих файлов не
    встречается, поэтому всё, что стоит до неё, – имя из ведомости Moodle.
    Четвёртое слово допускаем только здесь: «Абдуллаев Али Гасан оглы» из
    ведомости приходит целиком, а на титульном листе так не пишут.
    """
    stem = Path(filename).stem
    m = MOODLE_NAME_RE.match(stem)
    if m:
        cand = _titlecase(_clean_name(m.group(1)))
        if _looks_like_person(cand, max_words=4):
            return cand
    # Без связки имя ничем не отличается от названия работы, и «Основы СКС_ЛР1»
    # уходило в ведомость фамилией. Здесь требуем отчество или инициалы –
    # «Смирнова Елена_ЛР1» разберём уже по титульному листу.
    m = PLAIN_NAME_RE.match(stem)
    if m:
        cand = _titlecase(_clean_name(m.group(1)))
        parts = cand.split()
        if _looks_like_person(cand) and any(
                _PATRONYMIC_RE.search(p) or _INITIALS_RE.match(p)
                or p.lower() in _NAME_PARTICLES for p in parts):
            return cand
    return ''


def _name_from_text(title_text: str) -> str:
    """ФИО с титульного листа – запасной путь, когда имя файла молчит."""
    # Case-sensitive name classes (keywords are (?i:...)-insensitive locally):
    # a global IGNORECASE would make [А-ЯЁ][а-яё]+ match any word.
    NAME3   = r'[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2}'    # Фамилия Имя [Отчество]
    NAME_UP = r'[А-ЯЁ]{2,}(?:\s+[А-ЯЁ]{2,}){1,2}'          # ФАМИЛИЯ ИМЯ [ОТЧЕСТВО]
    FAM_IO  = r'[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*(?:[А-ЯЁ]\.)?'  # Фамилия И.[О.]
    IO_FAM  = r'[А-ЯЁ]\.\s*(?:[А-ЯЁ]\.)?\s*[А-ЯЁ][а-яё]+'  # И.[О.] Фамилия
    DOER    = r'(?i:выполнил[аи]?|подготовил[аи]?|разработал[аи]?|студент(?:ка)?|автор)'

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
            cand = _titlecase(_clean_name(m.group(1)))
            if _looks_like_person(cand):
                return cand
    return ''


# ────────────────────────────── Группа ──────────────────────────────────────
#
# Номер группы пишут и через дефисы – «КА-22-06», – и слитно – «КА2206».
# Границу кода задаём вручную: подчёркивание входит в \w, и \b внутри
# «Козлов_КА2206_ЛР1» не срабатывает, а именно так выглядят имена из Moodle.
_GRP_START = r'(?<![0-9A-Za-zА-Яа-яЁё])'
_GRP_END = r'(?![0-9A-Za-zА-Яа-яЁё])'
GROUP_SEP_RE = re.compile(
    rf'{_GRP_START}([А-ЯЁA-Za-z]{{1,5}})[-–_](\d{{2}})[-–_](\d{{2,3}}){_GRP_END}')
# Слитную запись опознаём только по заглавным буквам, иначе группой становится
# «Отчет2024». Заглавные – норма для кода группы и редкость для слова.
GROUP_RUN_RE = re.compile(
    rf'{_GRP_START}([А-ЯЁA-Z]{{2,5}})(\d{{2}})(\d{{2,3}}){_GRP_END}')


def _group_run(text: str) -> str:
    """Первая слитно записанная группа: «Козлов_КА2206_ЛР1» → «КА-22-06».

    Четыре цифры, складывающиеся в календарный год, – это год работы, а не
    номер группы: «СКС2024» и «Отчет_2025» группой не считаем.
    """
    for m in GROUP_RUN_RE.finditer(text):
        digits = m.group(2) + m.group(3)
        if len(digits) == 4 and 1990 <= int(digits) <= 2035:
            continue
        return f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
    return ''


def _identify_student(text_by_page: list, filename: str) -> dict:
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

    # ФИО: сначала имя файла, титульный лист – только если оттуда не вышло.
    student['name'] = _name_from_filename(filename) or _name_from_text(title_text)

    # Group
    for pat in [
        r'(?:группы?\s+)([А-ЯЁA-Za-z]{1,5}[-–]\d{2}[-–]\d{2,3})',
        r'\b([А-ЯЁA-Za-z]{1,5}[-–]\d{2}[-–]\d{2,3})\b',
    ]:
        m = re.search(pat, title_text)
        if m:
            student['group'] = m.group(1).strip()
            break
    # Слитная запись на титульном листе – только рядом со словом «группа»:
    # без него в «КА2206» превращается любое четырёхзначное число с буквами.
    if not student['group']:
        m = re.search(r'(?i:групп[аы])\s+([А-ЯЁA-Z]{2,5}\d{4,5})', title_text)
        if m:
            student['group'] = _group_run(m.group(1))

    # Fallback group from filename
    if not student['group']:
        stem = Path(filename).stem
        m = GROUP_SEP_RE.search(stem)
        student['group'] = (f'{m.group(1)}-{m.group(2)}-{m.group(3)}' if m
                            else _group_run(stem))

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
