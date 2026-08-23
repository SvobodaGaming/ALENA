"""HTML report generation with support for historical (memory) reports."""
import re
import base64
import html as _html
from functools import lru_cache
from pathlib import Path
from datetime import datetime

from . import branding, grading


FONTS_DIR = Path(__file__).resolve().parent.parent / 'static' / 'fonts'


@lru_cache(maxsize=1)
def _font_css() -> str:
    """@font-face со вшитым Manrope – тем же, что и в интерфейсе.

    Отчёт обязан быть самодостаточным: его открывают файлом с диска, а
    WeasyPrint печатает PDF с base_url на папке отчётов – ссылка на
    /static/... не разрешится ни там, ни там.

    Файл здесь один на кириллицу и латиницу, а не четыре сабсета, как в
    static/app.css. Разложенные по unicode-range сабсеты WeasyPrint рисует
    верно, но путает таблицу ToUnicode на стыке алфавитов: в готовом PDF
    «report» копировалось как «reporл». Один шрифт эту склейку убирает и
    вдобавок весит меньше двух сабсетов. Ось насыщенности сохранена –
    начертания от 400 до 800 берутся из него же.
    """
    f = FONTS_DIR / 'manrope-report.woff2'
    if not f.exists():
        return ''
    b64 = base64.b64encode(f.read_bytes()).decode('ascii')
    return ("@font-face{font-family:'Manrope';font-style:normal;"
            "font-weight:200 800;font-display:swap;"
            f"src:url(data:font/woff2;base64,{b64}) format('woff2');}}")


def _esc(s: str) -> str:
    return _html.escape(str(s))


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение числительного: 1 работа, 2 работы, 5 работ."""
    if n % 100 // 10 == 1:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _page_title(reports: list, now: str) -> str:
    """
    Заголовок вкладки. Одно только время не помогало: у преподавателя открыт
    десяток отчётов, и различать их надо по группе и объёму партии.
    """
    n = len(reports)
    groups = {g for r in reports if (g := (r.get('student') or {}).get('group', '').strip())}

    parts = [branding.APP_TITLE]
    if len(groups) == 1:
        parts.append(next(iter(groups)))
    elif len(groups) > 1:
        parts.append(f'{len(groups)} {_plural(len(groups), "группа", "группы", "групп")}')
    parts.append(f'{n} {_plural(n, "работа", "работы", "работ")}')
    parts.append(now.split()[0])        # только дата: время во вкладке лишнее
    return ' · '.join(parts)


def _cell_inline_style(sim: float, threshold: float) -> str:
    """Compute inline background for matrix cell, works in PDF (no JS needed)."""
    # Прозрачность считается здесь, поэтому цвет – числом, а не токеном:
    # rgba(180,35,24) – это --danger, rgba(184,134,11) – --attention.
    if sim >= threshold:
        intensity = min(1.0, 0.3 + (sim - threshold) / max(1 - threshold, 0.001) * 0.7)
        # Насыщенную заливку белый текст перекрывает в любой теме, а по
        # бледной цвет текста должен идти за темой – отсюда токен, а не
        # литеральный тёмный: на тёмном фоне он был бы нечитаем.
        text_color = '#fff' if intensity > 0.55 else 'var(--ink-strong)'
        return (f'background:rgba(180,35,24,{intensity:.2f});'
                f'color:{text_color};font-weight:600;')
    if sim >= threshold * 0.55:
        intensity = (sim - threshold * 0.55) / (threshold * 0.45 + 0.001) * 0.45
        return f'background:rgba(184,134,11,{intensity:.2f});'
    return ''


def _display_name(report: dict) -> str:
    s = report.get('student', {})
    name  = s.get('name',  '')
    group = s.get('group', '')
    if name and group:
        return f'{name} ({group})'
    if name or group:
        return name or group
    # Fallback: use stored filename or derive from path
    fname = report.get('filename', '')
    if fname:
        return Path(fname).stem[:60]
    path = report.get('path', '')
    if path.startswith('memory://'):
        return path[9:][:60]
    return Path(path).stem[:60]


def _short_name(report: dict) -> str:
    """Abbreviated name for matrix labels (row/column headers)."""
    s = report.get('student', {})
    name  = s.get('name',  '')
    group = s.get('group', '')
    if name:
        parts = name.split()
        abbr = parts[0] + (' ' + parts[1][0] + '.' if len(parts) > 1 else '')
        base = abbr + (f' {group}' if group else '')
    else:
        path = report.get('path', '')
        fname = report.get('filename', '')
        stem = Path(fname).stem if fname else (path[9:] if path.startswith('memory://') else Path(path).stem)
        base = stem[:25]
    if report.get('is_historical'):
        base += f' (v{report.get("historical_version", "?")})'
    return base


def _report_anchors(reports: list, job_id: str = '') -> dict:
    """Card ids unique by construction within one generated report."""
    prefix = re.sub(r'[^a-zA-Z0-9_-]', '_', str(job_id)) if job_id else 'report'
    return {id(report): f'r_{prefix}_{i}'
            for i, report in enumerate(reports, 1)}


def _anchor(report: dict, anchors: dict) -> str:
    if report.get('is_historical'):
        return ''   # historical reports have no card
    return anchors.get(id(report), '')


def _gost_score(gost_results: list) -> tuple:
    passed = sum(1 for c in gost_results if c['passed'])
    return passed, len(gost_results)


def _max_sim(path: str, matrix: dict) -> tuple:
    """Return (max_similarity, other_path) across all comparisons in the matrix."""
    row = matrix.get(path, {})
    best_sim, best_path = 0.0, None
    for other_path, sim in row.items():
        if other_path != path and sim > best_sim:
            best_sim, best_path = sim, other_path
    return best_sim, best_path


def _render_matrix(new_reports: list, historical_relevant: list,
                   text_plagiarism: dict, threshold: float) -> str:
    matrix = text_plagiarism.get('matrix', {})
    no_text = set(text_plagiarism.get('no_text') or ())
    all_reports = new_reports + historical_relevant
    if not matrix or len(all_reports) < 2:
        return '<p style="color:var(--muted-soft)">Недостаточно отчётов для матрицы.</p>'

    threshold_pct = int(threshold * 100)
    hist_paths = {r['path'] for r in historical_relevant}
    report_by_path = {r['path']: r for r in all_reports}
    paths = [r['path'] for r in all_reports]
    n = len(paths)

    # Server-side sizing so the matrix always fits the PDF page width.
    # A4 portrait content width ≈ 645px (210mm − 2×20mm margins at 96dpi).
    AVAIL = 645
    label_w = 150 if n <= 14 else (120 if n <= 22 else 96)
    col_w = max(15, min(30, (AVAIL - label_w) // n))
    show_pct = col_w >= 22
    cell_font = 8 if col_w >= 19 else 7
    row_font = 10 if label_w >= 120 else 8

    # Rotated column labels: cap length so they fit the header band height.
    col_label = {p: _short_name(report_by_path[p]) for p in paths}
    max_len = min(20, max((len(s) for s in col_label.values()), default=4))
    header_h = min(150, max(64, 26 + int(max_len * 6.0)))
    col_font = 9 if max_len <= 16 else 8

    def _clip(s: str, k: int) -> str:
        return s if len(s) <= k else s[:k - 1] + '…'

    def _th_style(p):
        if p in hist_paths:
            return ' style="background:var(--attention-soft);color:var(--attention);"'
        return ''

    # Column headers: name rotated 90° (reads bottom-to-top), narrow fixed cell.
    headers = ''.join(
        f'<th class="mh"{_th_style(p)} '
        f'title="{_esc(_display_name(report_by_path[p]))}">'
        f'<span>{_esc(_clip(col_label[p], max_len))}</span></th>'
        for p in paths
    )

    rows = []
    for p1 in paths:
        is_h1 = p1 in hist_paths
        row_th_style = ' style="background:var(--attention-soft);color:var(--attention);"' if is_h1 else ''
        name1 = _esc(_short_name(report_by_path[p1]))
        cells = []
        for p2 in paths:
            if p1 == p2:
                cells.append('<td class="mc cell-self">·</td>')
            elif p1 in hist_paths and p2 in hist_paths:
                cells.append('<td class="mc cell-self" style="color:var(--rule-strong);">·</td>')
            elif p1 in no_text or p2 in no_text:
                # Пара не сравнивалась: из одной из работ текст не извлёкся.
                # Ноль здесь означал бы «проверено, чисто» – это неправда.
                cells.append('<td class="mc" style="background:var(--info-soft);'
                             'color:var(--info);" title="текст не извлечён">–</td>')
            else:
                sim = matrix.get(p1, {}).get(p2, 0.0)
                pct = int(sim * 100)
                style = _cell_inline_style(sim, threshold)
                txt = f'{pct}%' if show_pct else f'{pct}'
                cells.append(f'<td class="mc matrix-cell" style="{style}">{txt}</td>')
        rows.append(
            f'<tr><th class="rh" title="{name1}"{row_th_style}>{name1}</th>'
            f'{"".join(cells)}</tr>'
        )

    # Per-matrix dimensions (depend on N) injected as a scoped style block.
    dims = (
        '<style>'
        f'.matrix-table thead th.mh{{width:{col_w}px;height:{header_h}px;}}'
        f'.matrix-table thead th.mh>span{{font-size:{col_font}px;}}'
        f'.matrix-table td.mc{{width:{col_w}px;font-size:{cell_font}px;}}'
        f'.matrix-table th.rh,.matrix-table th.corner{{width:{label_w}px;}}'
        f'.matrix-table th.rh{{font-size:{row_font}px;}}'
        '</style>'
    )

    hist_note = ''
    if historical_relevant:
        hist_note = (
            '<span style="display:inline-block;width:14px;height:14px;'
            'background:var(--attention-soft);border:1px solid var(--attention);border-radius:var(--radius-sm);vertical-align:middle;"></span>'
            ' Строки/столбцы на жёлтом – отчёты из базы предыдущих сессий &nbsp;'
        )

    return f'''{dims}
<div class="matrix-scroll">
<table class="matrix-table">
  <thead><tr><th class="corner"></th>{headers}</tr></thead>
  <tbody>{"".join(rows)}</tbody>
</table>
</div>
<p style="font-size:var(--text-13);color:var(--muted-soft);margin-top:var(--space-2);">
  <span style="display:inline-block;width:14px;height:14px;background:var(--danger);border-radius:var(--radius-sm);vertical-align:middle;"></span> ≥{threshold_pct}% – заимствование &nbsp;
  <span style="display:inline-block;width:14px;height:14px;background:var(--attention);border-radius:var(--radius-sm);vertical-align:middle;"></span> {int(threshold_pct*0.55)}–{threshold_pct}% – близко &nbsp;
  {'<span style="display:inline-block;width:14px;height:14px;background:var(--info-soft);border:1px solid var(--info);border-radius:var(--radius-sm);vertical-align:middle;"></span> «–» – текст не извлечён, сравнение невозможно &nbsp;' if no_text else ''}
  {hist_note}
</p>'''


# Сколько пар изображений показывать. Превью каждой картинки лежит в HTML
# целиком, поэтому пара весит десяток килобайт. Курс из полусотни работ с
# одинаковыми скриншотами даёт десятки тысяч пар – полный список раздувает
# отчёт до гигабайтов: сервер не собирает его, а браузер не открывает. Пары
# посчитаны все, выводятся самые близкие.
SUMMARY_PAIRS = 200      # в общем разделе «Дублирование изображений»
CARD_PAIRS    = 12       # в карточке одной работы


def _pairs_note(shown: int, total: int, where: str) -> str:
    if shown >= total:
        return ''
    return (f'<p style="color:var(--muted);font-size:var(--text-13);margin:var(--space-1) 0 var(--space-2);">'
            f'Показаны {shown} самых близких пар из {total}. Остальные учтены '
            f'в счётчиках{where}, но не выведены: с полным списком отчёт '
            f'весил бы сотни мегабайт и не открылся бы.</p>')


def _by_importance(pairs: list) -> list:
    """Сначала совпадения, потом «похожий интерфейс»; внутри – от близких к
    далёким (список приходит отсортированным по расстоянию)."""
    return ([p for p in pairs if not p.get('ui_review')]
            + [p for p in pairs if p.get('ui_review')])


def _render_image_summary(image_plagiarism: dict, report_by_path: dict) -> str:
    pairs = image_plagiarism.get('pairs', [])
    confirmed = [p for p in pairs if not p.get('ui_review')]
    review    = [p for p in pairs if p.get('ui_review')]

    head_badges = []
    if confirmed:
        head_badges.append(f'<span class="badge badge-red">{len(confirmed)} пар</span>')
    if review:
        head_badges.append(f'<span class="badge badge-amber">{len(review)} на ручную проверку</span>')
    if not pairs:
        head_badges.append('<span class="badge badge-green">не найдено</span>')

    if not pairs:
        body = ('<p style="color:var(--success);font-weight:600;">'
                '✓ Одинаковых изображений между отчётами не найдено.</p>')
        return f'''<div class="section">
  <div class="section-head">
    <h2 style="margin:0;">Дублирование изображений {' '.join(head_badges)}</h2>
    <span class="toggle-arrow">▼</span>
  </div>
  <div class="section-body">{body}</div>
</div>'''

    shown = _by_importance(pairs)[:SUMMARY_PAIRS]

    items = []
    for p in shown:
        r1 = report_by_path.get(p['report1'], {'path': p['report1']})
        r2 = report_by_path.get(p['report2'], {'path': p['report2']})
        n1 = _esc(_display_name(r1))
        n2 = _esc(_display_name(r2))

        def _name_with_badge(rep, name):
            if rep.get('is_historical'):
                v = rep.get('historical_version', '?')
                d = rep.get('historical_date', '')
                return (f'{name} <span class="badge badge-amber">'
                        f'база v{_esc(v)}{f", {_esc(d)}" if d else ""}</span>')
            return name

        n1_html = _name_with_badge(r1, n1)
        n2_html = _name_with_badge(r2, n2)

        if p.get('ui_review'):
            match_badge = '<span class="badge badge-blue">похожий интерфейс – проверьте вручную</span>'
        elif p.get('is_crop'):
            match_badge = '<span class="badge badge-amber">обрезанная копия</span>'
        else:
            match_badge = '<span class="badge badge-red">точная копия</span>'

        # src экранируется: превью приходит и из базы отпечатков, а та могла
        # быть залита чужим SQL-дампом – своей строке base64 экранирование не
        # мешает, а подставленной кавычке закрывает дорогу.
        img1_html = (f'<img src="{_esc(p["img1"])}" alt="img1">' if p.get('img1')
                     else '<div class="img-blank">нет превью</div>')
        img2_html = (f'<img src="{_esc(p["img2"])}" alt="img2">' if p.get('img2')
                     else '<div class="img-blank">нет превью</div>')

        items.append(f'''
<div class="img-pair">
  <div>
    {img1_html}
    <div class="img-info">{n1_html}<br>стр. {p["page1"]}</div>
  </div>
  <div style="align-self:center;font-size:var(--text-24);color:var(--danger);">≈</div>
  <div>
    {img2_html}
    <div class="img-info">{n2_html}<br>стр. {p["page2"]}</div>
  </div>
  <div class="img-info" style="align-self:center;">
    {match_badge}<br>
    <span style="color:var(--muted-soft);font-size:var(--text-12);">расст. {p["distance"]}/144</span>
  </div>
</div>''')

    review_note = ''
    if review:
        review_note = ('<p style="color:var(--muted);font-size:var(--text-13);margin:var(--space-1) 0 var(--space-2);">'
                       'Пары «похожий интерфейс» – это скриншоты одинаковых программ '
                       '(терминал, Zabbix и т.п.): совпадение оформления ожидаемо, '
                       'в статистику заимствований они не входят.</p>')

    return f'''<div class="section">
  <div class="section-head">
    <h2 style="margin:0;">Дублирование изображений {' '.join(head_badges)}</h2>
    <span class="toggle-arrow">▼</span>
  </div>
  <div class="section-body">
  {review_note}
  {_pairs_note(len(shown), len(pairs), ' и в карточках работ')}
  {''.join(items)}
  </div>
</div>'''


def _render_gost_table(gost_results: list) -> str:
    """Таблица критериев ГОСТ: знак, название, подробности.

    Знаки – круг, треугольник и косой крест, а не «✓ ✗ ⚠». Последних нет
    ни в Manrope, ни в шрифтах, которые находит WeasyPrint на сервере без
    установленного набора символов: в выгруженном PDF колонка выходила
    пустой, и таблица переставала что-либо значить. Взятые знаки есть в
    любом шрифте и различаются формой, а не только цветом.
    """
    rows = []
    for c in gost_results:
        if c['passed']:
            icon = '<span class="check-pass" title="соответствует">&#9679;</span>'
        elif c['severity'] == 'warning':
            icon = '<span class="check-warn" title="проверьте вручную">&#9650;</span>'
        else:
            icon = '<span class="check-fail" title="нарушение">&#215;</span>'
        details = (f'<span style="color:var(--muted);font-size:var(--text-13);">{_esc(c["details"])}</span>'
                   if c['details'] else '')
        rows.append(
            f'<tr><td class="check-cell">{icon}</td><td><b>{_esc(c["name"])}</b></td>'
            f'<td>{details}</td></tr>'
        )
    return (
        '<table class="checks-table">'
        '<thead><tr><th style="width:28px"></th><th>Проверка</th><th>Подробности</th></tr></thead>'
        '<tbody>' + ''.join(rows) + '</tbody></table>'
    )


NO_TEXT_NOTE = (
    '<div class="plagiarism-alert" style="background:var(--info-soft);border-color:var(--info);">'
    '<span class="badge badge-blue">ТЕКСТ НЕ ИЗВЛЕЧЁН</span> '
    'Из файла удалось прочитать слишком мало текста – обычно это скан или '
    'нестандартные шрифты. Сравнение с другими работами не проводилось: '
    'проверьте заимствование вручную.</div>')


def _render_text_plag_for_report(path: str, text_plagiarism: dict, threshold: float,
                                  report_by_path: dict, anchors: dict) -> str:
    if path in set(text_plagiarism.get('no_text') or ()):
        return NO_TEXT_NOTE

    matrix = text_plagiarism.get('matrix', {})
    pairs  = text_plagiarism.get('pairs', [])

    sims = [
        (other, sim)
        for other, sim in matrix.get(path, {}).items()
        if other != path
    ]
    sims.sort(key=lambda x: -x[1])

    if not sims:
        return '<p style="color:var(--muted-soft);font-size:var(--text-14);">Нет данных.</p>'

    max_other, max_sim = sims[0]

    if max_sim < threshold * 0.3:
        return (
            '<p style="color:var(--success);font-weight:600;font-size:var(--text-14);">'
            f'✓ Заимствования не обнаружено (макс. схожесть {max_sim:.0%})</p>'
        )

    parts = []
    for other_path, sim in sims[:5]:
        if sim < threshold * 0.3:
            continue
        other_rep   = report_by_path.get(other_path, {'path': other_path})
        other_name  = _esc(_display_name(other_rep))
        is_hist     = other_rep.get('is_historical', False)

        # Цвет плашки идёт за значком: красная – выше порога, жёлтая – ниже
        # и всё, что пришло из базы прошлых сессий (её же цветом помечены
        # строки матрицы). Раньше плашка всегда была красной, и «Близко» с
        # жёлтым значком лежало на красном фоне.
        near = sim < threshold
        if is_hist:
            hist_ver  = other_rep.get('historical_version', '?')
            hist_date = other_rep.get('historical_date', '')
            badge_cls = 'badge-red' if sim >= threshold else 'badge-amber'
            label     = 'ИЗ БАЗЫ' if sim >= threshold else 'База (близко)'
            ref_html  = (
                f'{other_name} '
                f'<span style="font-size:var(--text-13);">'
                f'(база v{hist_ver}, {hist_date})</span>'
            )
            alert_cls = ' hist'
        else:
            anchor    = _anchor(other_rep, anchors)
            badge_cls = 'badge-red' if sim >= threshold else 'badge-amber'
            label     = 'ЗАИМСТВОВАНИЕ' if sim >= threshold else 'Близко'
            ref_html  = (f'<a href="#{anchor}">{other_name}</a>'
                         if anchor else other_name)
            alert_cls = ' near' if near else ''

        passages_html = ''
        for pair in pairs:
            if {pair['report1'], pair['report2']} == {path, other_path}:
                if pair['passages']:
                    items = ''.join(
                        f'<div class="passage">{_esc(p[:280])}…</div>'
                        for p in pair['passages'][:3]
                    )
                    passages_html = f'<div style="margin-top:var(--space-2);">{items}</div>'
                break

        parts.append(
            f'<div class="plagiarism-alert{alert_cls}" style="margin-bottom:var(--space-2);">'
            f'<span class="badge {badge_cls}">{label}</span> '
            f'<strong>{sim:.0%}</strong> совпадений с '
            f'{ref_html}'
            f'{passages_html}</div>'
        )

    return ''.join(parts) if parts else (
        '<p style="color:var(--attention);font-size:var(--text-14);">'
        f'Схожесть до {max_sim:.0%}, ниже порога {threshold:.0%}</p>'
    )


def _render_img_plag_for_report(path: str, image_plagiarism: dict,
                                 report_by_path: dict) -> str:
    my_pairs = [
        p for p in image_plagiarism.get('pairs', [])
        if p['report1'] == path or p['report2'] == path
    ]
    if not my_pairs:
        return '<p style="color:var(--success);font-weight:600;font-size:var(--text-14);">✓ Дублей изображений нет</p>'

    confirmed = [p for p in my_pairs if not p.get('ui_review')]
    review    = [p for p in my_pairs if p.get('ui_review')]
    shown     = _by_importance(my_pairs)[:CARD_PAIRS]

    items = []
    for p in shown:
        is_mine_first = p['report1'] == path
        other_path = p['report2'] if is_mine_first else p['report1']
        other_page = p['page2']   if is_mine_first else p['page1']
        my_page    = p['page1']   if is_mine_first else p['page2']
        my_img     = p['img1']    if is_mine_first else p['img2']
        other_img  = p['img2']    if is_mine_first else p['img1']

        other_rep  = report_by_path.get(other_path, {'path': other_path})
        other_name = _esc(_display_name(other_rep))
        is_hist    = other_rep.get('is_historical', False)

        if is_hist:
            v = other_rep.get('historical_version', '?')
            d = other_rep.get('historical_date', '')
            other_label = (
                f'{other_name} '
                f'<span class="badge badge-amber">'
                f'база v{_esc(v)}{f", {_esc(d)}" if d else ""}</span>'
                f'<br>стр.{other_page}'
            )
        else:
            other_label = f'{other_name}, стр.{other_page}'

        other_img_html = (
            f'<img src="{_esc(other_img)}" alt="other" style="max-height:100px;">'
            if other_img else
            '<div class="img-blank" style="width:100px;height:70px;">нет превью</div>'
        )

        review_badge = ''
        if p.get('ui_review'):
            review_badge = (
                '<div style="align-self:center;">'
                '<span class="badge badge-blue">похожий интерфейс – проверьте вручную</span>'
                '</div>'
            )

        items.append(
            f'<div class="img-pair" style="margin:var(--space-2) 0;">'
            f'<div><img src="{_esc(my_img)}" alt="my" style="max-height:100px;">'
            f'<div class="img-info">Эта работа, стр.{my_page}</div></div>'
            f'<div style="align-self:center;color:var(--danger);font-size:var(--text-20);">≈</div>'
            f'<div>{other_img_html}'
            f'<div class="img-info">{other_label}</div></div>'
            f'{review_badge}'
            f'</div>'
        )

    badges = []
    if confirmed:
        badges.append(f'<span class="badge badge-red">{len(confirmed)} дублей</span>')
    if review:
        badges.append(f'<span class="badge badge-blue">{len(review)} на ручную проверку</span>')
    return (' '.join(badges) + _pairs_note(len(shown), len(my_pairs), '')
            + ''.join(items))


def _render_feedback(report: dict, gost_results: list, max_sim: float,
                     threshold: float, weights: dict, scale: int,
                     no_text: bool = False) -> str:
    """Рекомендуемая оценка и готовый к копированию отзыв для одной работы."""
    mark = grading.grade(gost_results, weights, scale)
    student = {
        'fio':     _display_name(report),
        'group':   (report.get('student') or {}).get('group', ''),
        'flaws':   grading.flaws(gost_results),
        'plag':    None if no_text else round(max_sim * 100),
        'no_text': no_text,
        'grade':   mark,
    }
    thr_pct = int(threshold * 100)
    lines = grading.feedback_lines(student, thr_pct)
    plain = grading.feedback_text(student, thr_pct)

    if mark['pct'] is None:
        pct_text, color = '–', 'var(--info)'
    else:
        pct = mark['pct']
        pct_text = f'{pct}%'
        color = 'var(--success)' if pct >= 85 else 'var(--attention)' if pct >= 60 else 'var(--danger)'
    in_points = (f' &nbsp;<span style="font-size:var(--text-13);color:var(--muted);">'
                 f'{mark["score"]:g} из {mark["scale"]}</span>'
                 if mark['score'] is not None else '')

    if lines:
        items = ''.join(f'<li>{_esc(l)}</li>' for l in lines)
        body = f'<ul class="flaw-list">{items}</ul>'
    else:
        body = ('<p style="color:var(--success);font-size:var(--text-14);margin:0;">'
                'Замечаний по оформлению нет.</p>')

    costly = ''
    if mark['lost']:
        top = mark['lost'][0]
        costly = (f'<p class="flaw-note">Дороже всего обошлось: '
                  f'{_esc(top["name"])} – минус {top["weight"]:g}%.</p>')

    return f'''
    <div class="verdict">
      <div class="verdict-head">
        <h3 style="margin:0;">Рекомендуемая оценка за оформление</h3>
        <span class="verdict-grade" style="color:{color};">{pct_text}</span>{in_points}
        <button type="button" class="copy-btn" data-text="{_esc(plain)}">Копировать отзыв</button>
      </div>
      {body}
      {costly}
    </div>'''


def _render_card(report: dict, text_plagiarism: dict, image_plagiarism: dict,
                 threshold: float, report_by_path: dict,
                 anchors: dict,
                 weights: dict = None, scale: int = grading.DEFAULT_SCALE) -> str:
    path = report['path']
    gost_results = report.get('gost_results', [])
    passed, total = _gost_score(gost_results)
    score_pct = int(passed / total * 100) if total else 0

    matrix = text_plagiarism.get('matrix', {})
    max_sim, _ = _max_sim(path, matrix)
    no_text = path in set(text_plagiarism.get('no_text') or ())

    has_text_plag = max_sim >= threshold and not no_text
    has_img_plag  = any(
        (p['report1'] == path or p['report2'] == path) and not p.get('ui_review')
        for p in image_plagiarism.get('pairs', [])
    )

    if has_text_plag or has_img_plag:
        badge = '<span class="badge badge-red">Заимствование</span>'
        header_border = 'border-left:4px solid var(--danger);'
    elif no_text:
        badge = '<span class="badge badge-blue">Текст не извлечён</span>'
        header_border = 'border-left:4px solid var(--info);'
    elif total and passed < total * 0.7:
        badge = '<span class="badge badge-amber">Нарушения ГОСТ</span>'
        header_border = 'border-left:4px solid var(--attention);'
    elif not total:
        # Критерии сняты все до единого – сказать «OK» не о чем.
        badge = '<span class="badge badge-blue">ГОСТ не проверялся</span>'
        header_border = 'border-left:4px solid var(--info);'
    else:
        badge = '<span class="badge badge-green">OK</span>'
        header_border = 'border-left:4px solid var(--success);'

    score_color = ('var(--success)' if score_pct >= 85 else
                   'var(--attention)' if score_pct >= 60 else 'var(--danger)')

    s = report.get('student', {})
    meta_parts = []
    if s.get('group'):      meta_parts.append(f'Группа: <b>{_esc(s["group"])}</b>')
    if s.get('year'):       meta_parts.append(f'Год: {_esc(s["year"])}')
    if s.get('work_title'): meta_parts.append(_esc(s['work_title'][:80]))
    meta = ' &nbsp;|&nbsp; '.join(meta_parts)

    scan_warn = ''
    if report.get('is_scanned'):
        scan_warn = ('<div class="badge badge-amber" style="margin-bottom:var(--space-3);">'
                     '⚠ Возможно, отсканированный PDF, текст не извлечён</div>')

    fname = report.get('filename', '') or Path(path).name
    fname_disp = _esc(fname)

    if report.get('error'):
        return f'''
<div class="report-card" id="{_anchor(report, anchors)}">
  <div class="report-header" style="{header_border}">
    <span style="font-size:var(--text-16);font-weight:600;flex:1;">{_esc(_display_name(report))}</span>
    <span class="badge badge-red">Ошибка чтения</span>
  </div>
  <div class="report-body">
    <p style="color:var(--danger);">{_esc(report["error"])}</p>
    <p style="color:var(--muted);font-size:var(--text-13);">{fname_disp}</p>
  </div>
</div>'''

    gost_table = _render_gost_table(gost_results)
    text_plag  = _render_text_plag_for_report(
        path, text_plagiarism, threshold, report_by_path, anchors)
    img_plag   = _render_img_plag_for_report(path, image_plagiarism, report_by_path)

    return f'''
<div class="report-card" id="{_anchor(report, anchors)}">
  <div class="report-header" style="{header_border}">
    <span style="font-size:var(--text-16);font-weight:600;flex:1;">{_esc(_display_name(report))}</span>
    {badge}
    <span style="font-size:var(--text-13);color:var(--muted);white-space:nowrap;">
      ГОСТ: {passed}/{total} &nbsp;|&nbsp; Схожесть: {'–' if no_text else f'{max_sim:.0%}'}
    </span>
    <span class="toggle-arrow">▼</span>
  </div>
  <div class="report-body">
    {scan_warn}
    {f'<p style="color:var(--muted);font-size:var(--text-13);margin-bottom:var(--space-2);">{meta}</p>' if meta else ''}
    <p style="font-size:var(--text-12);color:var(--muted-soft);margin-bottom:var(--space-4);">📄 {fname_disp}</p>

    {_render_feedback(report, gost_results, max_sim, threshold, weights, scale, no_text)}

    <div class="grid-2">
      <!-- GOST -->
      <div>
        <h3>ГОСТ 7.32-2017</h3>
        <div class="score-bar" style="margin-bottom:var(--space-2);">
          <span style="font-size:var(--text-13);color:var(--muted);width:32px;">{score_pct}%</span>
          <div class="score-track">
            <div class="score-fill" style="width:{score_pct}%;background:{score_color};"></div>
          </div>
          <span style="font-size:var(--text-13);color:var(--muted);">{passed}/{total}</span>
        </div>
        {gost_table}
      </div>

      <!-- Plagiarism -->
      <div>
        <h3>Заимствование текста</h3>
        {text_plag}
        <div style="margin-top:var(--space-5);">
          <h3>Заимствование изображений</h3>
          {img_plag}
        </div>
      </div>
    </div>
  </div>
</div>'''


def generate_html_report(reports: list, historical: list,
                         text_plagiarism: dict, image_plagiarism: dict,
                         threshold: float = 0.6, job_id: str = '',
                         weights: dict = None,
                         scale: int = grading.DEFAULT_SCALE) -> str:
    """
    Generate a self-contained HTML report.

    Args:
        reports: newly checked reports (full report dicts with gost_results etc.)
        historical: virtual report dicts from memory store (is_historical=True)
        text_plagiarism, image_plagiarism: results from checker modules
        threshold: similarity threshold (0-1)
        weights: «процент использования» критериев для рекомендуемой оценки
        scale: шкала оценки (100 – проценты)
        job_id: идентификатор проверки для уникальных якорей и ссылки экспорта
    """
    now      = datetime.now().strftime('%d.%m.%Y %H:%M')
    n        = len(reports)
    thr_pct  = int(threshold * 100)
    page_title = _page_title(reports, now)
    anchors = _report_anchors(reports, job_id)

    new_paths  = {r['path'] for r in reports}
    hist_paths = {h['path'] for h in historical}

    # Filter historical to only those that have at least one relevant match
    matrix = text_plagiarism.get('matrix', {})
    img_pairs = image_plagiarism.get('pairs', [])
    # "похожий интерфейс" pairs are shown for manual review but do not count
    # as plagiarism in any statistic below
    img_confirmed = [p for p in img_pairs if not p.get('ui_review')]

    def _hist_has_match(h):
        hp = h['path']
        row = matrix.get(hp, {})
        if any(np in row and row[np] >= threshold * 0.25 for np in new_paths):
            return True
        if any((p['report1'] == hp and p['report2'] in new_paths) or
               (p['report2'] == hp and p['report1'] in new_paths)
               for p in img_pairs):
            return True
        return False

    historical_relevant = [h for h in historical if _hist_has_match(h)]
    all_reports = reports + historical_relevant
    report_by_path = {r['path']: r for r in all_reports}


    flagged_text = len({
        p for pair in text_plagiarism.get('pairs', [])
        for p in (pair['report1'], pair['report2'])
        if p in new_paths
    })
    flagged_img = len({
        p for pair in img_confirmed
        for p in (pair['report1'], pair['report2'])
        if p in new_paths
    })
    # Работа без единого критерия «полностью соответствует ГОСТ» не считается:
    # all([]) истинно, и при снятых до последнего критериях отчёт объявлял
    # соответствующими все работы, хотя не проверил ни одной.
    gost_full = sum(
        1 for r in reports
        if r.get('gost_results') and all(c['passed'] for c in r['gost_results'])
    )

    # Count new reports that matched something in the historical base
    cross_session = set()
    for pair in text_plagiarism.get('pairs', []):
        if pair['report1'] in hist_paths and pair['report2'] in new_paths:
            cross_session.add(pair['report2'])
        elif pair['report2'] in hist_paths and pair['report1'] in new_paths:
            cross_session.add(pair['report1'])
    for pair in img_confirmed:
        if pair['report1'] in hist_paths and pair['report2'] in new_paths:
            cross_session.add(pair['report2'])
        elif pair['report2'] in hist_paths and pair['report1'] in new_paths:
            cross_session.add(pair['report1'])
    cross_session_count = len(cross_session)


    matrix_html = _render_matrix(reports, historical_relevant, text_plagiarism, threshold)


    img_summary = _render_image_summary(image_plagiarism, report_by_path)


    cards = ''.join(
        _render_card(r, text_plagiarism, image_plagiarism, threshold,
                     report_by_path, anchors, weights, scale)
        for r in reports
    )


    no_text_paths = set(text_plagiarism.get('no_text') or ())

    def _row_class(r):
        path = r['path']
        sim, _ = _max_sim(path, matrix)
        has_plag = (sim >= threshold and path not in no_text_paths) or any(
            p['report1'] == path or p['report2'] == path for p in img_confirmed
        )
        p, t = _gost_score(r.get('gost_results', []))
        if has_plag:              return 'tr-red'
        if t and p < t * 0.7:     return 'tr-amber'
        return 'tr-green'

    summary_rows = []
    for r in reports:
        path = r['path']
        sim, sim_other = _max_sim(path, matrix)
        row_no_text = path in no_text_paths

        other_rep = report_by_path.get(sim_other) if sim_other else None
        if other_rep and other_rep.get('is_historical'):
            v = other_rep.get('historical_version', '?')
            d = other_rep.get('historical_date', '')
            other_name_html = (
                f'{_esc(_display_name(other_rep))} '
                f'<span style="color:var(--attention);font-size:var(--text-12);">(база v{v}, {d})</span>'
            )
        elif other_rep:
            anc = _anchor(other_rep, anchors)
            escaped_name = _esc(_display_name(other_rep))
            other_name_html = (f'<a href="#{anc}">{escaped_name}</a>'
                               if anc else escaped_name)
        else:
            other_name_html = '–'

        p, t = _gost_score(r.get('gost_results', []))
        img_count = sum(
            1 for pair in img_confirmed
            if pair['report1'] == path or pair['report2'] == path
        )
        review_count = sum(
            1 for pair in img_pairs
            if pair.get('ui_review')
            and (pair['report1'] == path or pair['report2'] == path)
        )
        if row_no_text:
            plag_badge = '<span class="badge badge-blue">текст не извлечён</span>'
            other_name_html = 'сравнение не проводилось'
        elif sim >= threshold:
            plag_badge = f'<span class="badge badge-red">{sim:.0%}</span>'
        else:
            plag_badge = f'<span style="color:var(--muted);">{sim:.0%}</span>'
        if not t:
            gost_badge = '<span class="badge badge-blue">не проверялся</span>'
        elif p == t:
            gost_badge = f'<span class="badge badge-green">{p}/{t}</span>'
        else:
            gost_badge = (f'<span class="badge '
                          f'{"badge-amber" if p >= t * 0.7 else "badge-red"}">{p}/{t}</span>')
        if img_count:
            img_badge = f'<span class="badge badge-red">{img_count} дублей</span>'
        elif review_count:
            img_badge = f'<span class="badge badge-blue">{review_count} на проверку</span>'
        else:
            img_badge = '<span style="color:var(--success);">–</span>'
        mark = grading.grade(r.get('gost_results', []), weights, scale)
        if mark['pct'] is None:
            # Ни одного критерия – оценивать нечего; «0 %» читалось бы как двойка.
            mark_badge = '<span class="badge badge-blue">–</span>'
        else:
            mark_pct = mark['pct']
            mark_text = (f'{mark["score"]:g} из {mark["scale"]}'
                         if mark['score'] is not None else f'{mark_pct}%')
            mark_badge = (
                f'<span class="badge badge-green">{mark_text}</span>' if mark_pct >= 85 else
                f'<span class="badge {"badge-amber" if mark_pct >= 60 else "badge-red"}">'
                f'{mark_text}</span>'
            )
        anchor = _anchor(r, anchors)
        summary_rows.append(
            f'<tr class="{_row_class(r)}">'
            f'<td><a href="#{anchor}">{_esc(_display_name(r))}</a></td>'
            f'<td>{plag_badge} → {other_name_html}</td>'
            f'<td>{img_badge}</td>'
            f'<td>{gost_badge}</td>'
            f'<td>{mark_badge}</td>'
            f'</tr>'
        )

    summary_table = f'''
<table class="summary-table">
  <thead>
    <tr>
      <th>Студент</th>
      <th>Макс. схожесть текста</th>
      <th>Дубли изображений</th>
      <th>ГОСТ</th>
      <th>Оценка за оформление</th>
    </tr>
  </thead>
  <tbody>{"".join(summary_rows)}</tbody>
</table>'''


    cross_card = ''
    if historical:
        cross_card = f'''
  <div class="stat-card {'c-red' if cross_session_count else 'c-green'}">
    <div class="stat-num">{cross_session_count}</div>
    <div class="stat-lbl">Совпадений с базой ({len(historical)} в базе)</div>
  </div>'''


    if job_id:
        dl_btn = f'<a href="/export/{job_id}" class="print-btn">&#11015; Скачать PDF</a>'
    else:
        dl_btn = ''


    # Логотип встраивается один раз – фоном для двух меток, иначе строка
    # base64 весом под 70 КБ лежала бы в файле дважды.
    logo = branding.logo_data_uri()
    logo_css = f'.logo-mark {{ background-image: url({logo}); }}' if logo else ''
    logo_img = '<span class="brand-logo logo-mark"></span>' if logo else ''
    acronym = ''.join(f'<li><b>{letter}</b>{word[1:]}</li>'
                      for letter, word in branding.ACRONYM)

    page_rule = f'''@page {{
  size: A4 portrait;
  margin: 15mm 20mm 24mm 20mm;
  /* Колонтитулы – литеральные цвета: поля страницы наследуются от контекста
     печати, и полагаться на то, что до них дойдут токены :root, не стоит.
     #8C968F – это --muted-soft. */
  @bottom-left {{
    content: "{branding.TEAM} · {branding.APP_TITLE} – {branding.APP_FULL_NAME}";
    font-size: 7.5pt;
    color: #8C968F;
    font-family: 'Manrope', Arial, sans-serif;
  }}
  @bottom-right {{
    content: "Сформировано: {now}  ·  стр. " counter(page) " / " counter(pages);
    font-size: 7.5pt;
    color: #8C968F;
    font-family: 'Manrope', Arial, sans-serif;
  }}
}}'''

    css = _font_css() + page_rule + '''
/* ─────────────────────────── Токены ───────────────────────────
   Тот же дизайн-код, что в интерфейсе (static/app.css): палитра, шкала
   размеров и отступов, радиусы и тени сняты с uralolimp.website (тема
   Scholaria). Имена токенов совпадают, поэтому правка палитры в одном
   месте переносится сюда копированием блока.

   Отличие от интерфейса одно: добавлен синий --info. В интерфейсе такого
   цвета нет, но здесь он несёт смысл, который серым не передать:
   «не проверено» (текст не извлёкся, пару нужно смотреть руками) – это не
   то же самое, что «проверено, чисто».

   Тем две, как в интерфейсе, и переключаются они тем же ключом
   localStorage ('alena-theme'): отчёт отдаётся с того же домена, поэтому
   выбранная в интерфейсе тема подхватывается сама. На бумаге и в PDF
   тема всегда светлая – блок @media print ниже возвращает эти же
   значения поверх тёмных. */
:root {
  color-scheme: light;

  --brand:        #015D1E;
  --brand-hover:  #024E19;
  --on-brand:     #FFFFFF;

  --ink:          #1A1F1B;
  --ink-strong:   #0B0F0C;
  --muted:        #5F6B62;
  --muted-soft:   #8C968F;
  --surface:      #FFFFFF;
  --surface-2:    #f6f8f6;
  --surface-3:    #edf1ee;
  --rule:         #D9DFDB;
  --rule-strong:  #B8C2BC;

  /* Две рабочие поверхности поверх фона страницы: карточка и утопленная
     подложка (шапки таблиц, плашка оценки, ховер). На белом фоне карточка
     тоже белая и держится рамкой, в тёмной теме она светлее страницы –
     иначе отчёт вышел бы плоским пятном. */
  --card:         #FFFFFF;
  --sunken:       #f6f8f6;

  --success:      #027A48;  --success-soft:   #DFF3E8;
  --attention:    #B8860B;  --attention-soft: #FAF1DC;
  --danger:       #B42318;  --danger-soft:    #FEE4E2;
  --info:         #1F5FA8;  --info-soft:      #E8EFF8;

  /* Тёмная плашка шапки – тот же рельс, что и в боковом меню. */
  --rail:         #04361a;
  --rail-ink:     #cfe3d6;
  --rail-strong:  #b7d6c2;
  --rail-muted:   #6f9c81;
  --rail-rule:    rgba(255,255,255,.12);

  --focus-ring:   0 0 0 3px rgba(1,93,30,.35);

  /* Цвет подчёркивания ссылок. Токеном, а не литеральным rgba в правиле:
     полупрозрачная тёмная зелень на тёмном фоне не видна. color-mix не
     годится – WeasyPrint функцию не считает и красит текст чёрным. */
  --uline:           rgba(1,93,30,.4);
  --uline-danger:    rgba(180,35,24,.4);
  --uline-attention: rgba(184,134,11,.4);

  --radius-sm:    4px;
  --radius:       8px;
  --radius-lg:    16px;
  --radius-pill:  999px;

  --space-1: 4px;  --space-2: 8px;  --space-3: 12px;
  --space-4: 16px; --space-5: 24px; --space-6: 32px;
  --space-7: 48px;

  /* Manrope вшит выше. Моноширинный – системный: он нужен только для
     цитат из работ, а вшивать ради них второй файл по 40 КБ в каждый
     отчёт не стоит. Цифры выравниваются через tabular-nums Manrope. */
  --font-sans: 'Manrope', system-ui, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, 'SF Mono', Consolas, 'Liberation Mono', Menlo, monospace;

  --text-11: .6875rem; --text-12: .75rem;  --text-13: .8125rem;
  --text-14: .875rem;  --text-15: .9375rem; --text-16: 1rem;
  --text-18: 1.125rem; --text-20: 1.25rem;  --text-24: 1.5rem;
  --text-30: 1.875rem;

  --leading-tight: 1.2; --leading-snug: 1.35; --leading-base: 1.6;
}

/* Тёмная тема – палитра интерфейса (static/app.css) плюс тёмный --info.
   Набор задан дважды: по системной настройке и по явному переключателю,
   как в app.css. Системный блок ограничен @media screen – WeasyPrint
   печатает в media print и до него не доходит, а атрибута data-theme в
   сохранённом файле нет (его ставит JS), так что PDF остаётся светлым. */
@media screen and (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --brand:#4FB069; --brand-hover:#6CC987; --on-brand:#0B130D;
    --ink:#E8ECE9; --ink-strong:#FFFFFF; --muted:#9AA59E; --muted-soft:#6F7A73;
    --surface:#0F1411; --surface-2:#161d18; --surface-3:#1f2822;
    --card:#161d18; --sunken:#1f2822;
    --rule:#2A332C; --rule-strong:#3A4640;
    --success:#3FBF92; --success-soft:#102A22;
    --attention:#E2B85C; --attention-soft:#2B2415;
    --danger:#F1746A; --danger-soft:#2E1715;
    --info:#7FAEE8; --info-soft:#14243A;
    --rail:#08130d; --rail-ink:#a9c6b4; --rail-strong:#b7d6c2;
    --rail-muted:#6f9c81; --rail-rule:rgba(255,255,255,.12);
    --focus-ring:0 0 0 3px rgba(79,176,105,.4);
    --uline:rgba(79,176,105,.45);
    --uline-danger:rgba(241,116,106,.45);
    --uline-attention:rgba(226,184,92,.45);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --brand:#4FB069; --brand-hover:#6CC987; --on-brand:#0B130D;
  --ink:#E8ECE9; --ink-strong:#FFFFFF; --muted:#9AA59E; --muted-soft:#6F7A73;
  --surface:#0F1411; --surface-2:#161d18; --surface-3:#1f2822;
  --card:#161d18; --sunken:#1f2822;
  --rule:#2A332C; --rule-strong:#3A4640;
  --success:#3FBF92; --success-soft:#102A22;
  --attention:#E2B85C; --attention-soft:#2B2415;
  --danger:#F1746A; --danger-soft:#2E1715;
  --info:#7FAEE8; --info-soft:#14243A;
  --rail:#08130d; --rail-ink:#a9c6b4; --rail-strong:#b7d6c2;
  --rail-muted:#6f9c81; --rail-rule:rgba(255,255,255,.12);
  --focus-ring:0 0 0 3px rgba(79,176,105,.4);
  --uline:rgba(79,176,105,.45);
  --uline-danger:rgba(241,116,106,.45);
  --uline-attention:rgba(226,184,92,.45);
}

/* ──────────────────────────── База ──────────────────────────── */

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: var(--font-sans);
  font-size: var(--text-15);
  line-height: var(--leading-base);
  background: var(--surface);
  color: var(--ink);
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
}
.container { max-width: 1200px; margin: 0 auto; padding: var(--space-6) var(--space-5) var(--space-7); }

h1, h2, h3 { font-weight: 700; color: var(--ink-strong); line-height: var(--leading-tight); letter-spacing: -.01em; }
h1 { font-size: var(--text-24); }
h2 { font-size: var(--text-18); margin: 0 0 var(--space-3); }
h3 { font-size: var(--text-14); margin: 0 0 var(--space-2); }
.subtitle { color: var(--muted); font-size: var(--text-13); margin: var(--space-1) 0 0; }

/* Подчёркнутые ссылки – как в образце. */
a {
  color: var(--brand);
  text-decoration-line: underline;
  text-decoration-thickness: 1px;
  text-underline-offset: 3px;
  text-decoration-color: var(--uline);
  transition: color .15s ease, text-decoration-color .15s ease;
}
a:hover { color: var(--brand-hover); text-decoration-color: var(--brand-hover); }
a.print-btn { text-decoration: none; }
::selection { background: var(--brand); color: var(--on-brand); }
:focus-visible { outline: none; box-shadow: var(--focus-ring); }
a:focus-visible { border-radius: 2px; }

/* Шапка и подвал: та же марка, что и в интерфейсе – логотип #au_team,
   название и расшифровка по буквам. */
.brand-head {
  display: flex; align-items: flex-start; gap: var(--space-4); flex-wrap: wrap;
  background: var(--rail); color: var(--rail-ink); border-radius: var(--radius-lg);
  padding: var(--space-4) var(--space-5); margin-bottom: var(--space-4);
}
.logo-mark {
  background-color: #fff; background-repeat: no-repeat; background-position: center;
  background-size: contain; background-origin: content-box; display: inline-block;
}
.brand-logo { width: 54px; height: 54px; flex: none; border-radius: var(--radius); padding: var(--space-1); }
.brand-head h1 { color: #fff; }
.brand-head .subtitle { color: var(--rail-muted); }
.brand-acronym {
  list-style: none; margin: 0 0 0 auto; padding: 0 0 0 var(--space-4);
  font-size: var(--text-11); line-height: var(--leading-snug);
  color: var(--rail-muted); border-left: 1px solid var(--rail-rule);
}
.brand-acronym b { color: var(--rail-strong); font-weight: 800; }
.brand-team { font-size: var(--text-12); color: var(--rail-muted); margin-top: var(--space-2); }
.brand-team b { color: var(--rail-strong); }

.brand-foot {
  display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap;
  margin-top: var(--space-6); padding: var(--space-3) var(--space-4);
  background: var(--card); border: 1px solid var(--rule); border-radius: var(--radius-lg);
  font-size: var(--text-12); color: var(--muted);
}
/* Подложка у метки белая, а страница теперь тоже белая – без рамки метка
   растворяется. */
.brand-foot .logo-mark {
  width: 32px; height: 32px; flex: none; padding: 2px;
  border: 1px solid var(--rule); border-radius: var(--radius-sm);
}
.brand-foot b { color: var(--ink-strong); }
.brand-foot .right { margin-left: auto; text-align: right; }

/* ───────────────────────── Компоненты ───────────────────────── */

/* Карточки держатся на рамке, а не на тени: страница белая, тень на ней
   выглядит грязью, а на печати ещё и съедает тонер. */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: var(--space-3); margin-bottom: var(--space-5); }
.stat-card {
  background: var(--card); border: 1px solid var(--rule);
  border-radius: var(--radius-lg); padding: var(--space-4);
}
.stat-num {
  font-size: var(--text-30); font-weight: 700; line-height: var(--leading-tight);
  letter-spacing: -.03em; font-variant-numeric: tabular-nums;
}
.stat-lbl { color: var(--muted); font-size: var(--text-12); margin-top: var(--space-1); line-height: var(--leading-snug); }
.c-blue .stat-num   { color: var(--brand); }
.c-red .stat-num    { color: var(--danger); }
.c-amber .stat-num  { color: var(--attention); }
.c-green .stat-num  { color: var(--success); }

.section {
  background: var(--card); border: 1px solid var(--rule);
  border-radius: var(--radius-lg); padding: var(--space-5); margin-bottom: var(--space-4);
}
.section-head { display: flex; align-items: center; gap: var(--space-2); cursor: pointer; }
.section-head h2 { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; margin: 0; }
.section-head .toggle-arrow { margin-left: auto; }
.section-body { display: none; margin-top: var(--space-3); }
.section-body.open { display: block; }

/* Таблицы – шапка как в интерфейсе: капитель, разрядка, приглушённый цвет. */
/* Пять колонок на узкий экран не встают – таблица уезжает вбок внутри
   карточки, а не режется по её краю. */
.tbl-wrap { overflow-x: auto; }
.summary-table { width: 100%; border-collapse: collapse; font-size: var(--text-14); }
.summary-table th {
  text-align: left; padding: var(--space-2) var(--space-3); background: var(--sunken);
  color: var(--muted); font-weight: 700; font-size: var(--text-12);
  letter-spacing: .06em; text-transform: uppercase;
  border-bottom: 1px solid var(--rule); white-space: nowrap;
}
.summary-table td { padding: var(--space-2) var(--space-3); border-bottom: 1px solid var(--surface-3); vertical-align: middle; }
.summary-table tbody tr:last-child td { border-bottom: 0; }
.tr-red   td:first-child { border-left: 3px solid var(--danger); }
.tr-amber td:first-child { border-left: 3px solid var(--attention); }
.tr-green td:first-child { border-left: 3px solid var(--success); }

.badge {
  display: inline-block; padding: 2px var(--space-2); border-radius: var(--radius-pill);
  font-size: var(--text-12); font-weight: 600; line-height: var(--leading-snug);
}
.badge-red   { background: var(--danger-soft);    color: var(--danger); }
.badge-amber { background: var(--attention-soft); color: var(--attention); }
.badge-green { background: var(--success-soft);   color: var(--success); }
.badge-blue  { background: var(--info-soft);      color: var(--info); }

.matrix-scroll { overflow-x: auto; }
.matrix-table { border-collapse: collapse; table-layout: fixed; margin: 0 auto; font-variant-numeric: tabular-nums; }
.matrix-table th, .matrix-table td { border: 1px solid var(--rule); }
.matrix-table td.mc { text-align: center; padding: 0; height: 18px; line-height: 1.1; overflow: hidden; }
.matrix-table thead th.mh { position: relative; background: var(--sunken); vertical-align: bottom; padding: 0; overflow: hidden; }
.matrix-table thead th.mh > span { position: absolute; bottom: 4px; left: 50%; transform-origin: left bottom; transform: rotate(-90deg); white-space: nowrap; font-weight: 600; line-height: 1; }
.matrix-table thead th.corner { background: var(--sunken); }
.matrix-table tbody th.rh { text-align: left; font-weight: 500; background: var(--sunken); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 2px var(--space-2); }
.cell-self { background: var(--surface-3) !important; }

.report-card {
  background: var(--card); border: 1px solid var(--rule);
  border-radius: var(--radius-lg); margin-bottom: var(--space-3); overflow: hidden;
}
.report-header { padding: var(--space-3) var(--space-4); display: flex; align-items: center; gap: var(--space-3); cursor: pointer; }
.report-header:hover { background: var(--sunken); }
.report-body { padding: var(--space-5); display: none; border-top: 1px solid var(--rule); }
.report-body.open { display: block; }
.toggle-arrow { color: var(--muted-soft); font-size: var(--text-12); transition: transform .2s; margin-left: auto; }
.toggle-arrow.open { transform: rotate(180deg); }

.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-5); }
@media (max-width: 860px) {
  .grid-2 { grid-template-columns: 1fr; }
  .container { padding: var(--space-4) var(--space-3) var(--space-6); }
}

.checks-table { width: 100%; border-collapse: collapse; font-size: var(--text-13); }
.checks-table th {
  text-align: left; padding: var(--space-1) var(--space-2); background: var(--sunken);
  color: var(--muted); font-weight: 700; font-size: var(--text-11);
  letter-spacing: .06em; text-transform: uppercase; border-bottom: 1px solid var(--rule);
}
.checks-table td { padding: var(--space-1) var(--space-2); border-bottom: 1px solid var(--surface-3); vertical-align: top; }
.checks-table tbody tr:last-child td { border-bottom: 0; }
.check-cell { text-align: center; line-height: 1.4; }
.check-pass { color: var(--success);   font-weight: 700; }
.check-fail { color: var(--danger);    font-weight: 700; font-size: var(--text-16); }
.check-warn { color: var(--attention); font-weight: 700; font-size: var(--text-12); }

.score-bar { display: flex; align-items: center; gap: var(--space-2); }
.score-track { flex: 1; height: 7px; background: var(--surface-3); border-radius: var(--radius-pill); overflow: hidden; }
.score-fill { height: 100%; border-radius: var(--radius-pill); }

/* Если ФИО не распозналось, работа подписана именем файла – строкой без
   пробелов длиной под 60 знаков. В узкой колонке карточки она вылезала за
   край, а на печати – за поле страницы. */
.plagiarism-alert, .img-info, .report-header, .summary-table td { overflow-wrap: break-word; }

.plagiarism-alert {
  background: var(--danger-soft); border: 1px solid var(--danger);
  border-radius: var(--radius); padding: var(--space-2) var(--space-3);
  margin-bottom: var(--space-2); font-size: var(--text-14);
}
/* Ниже порога и совпадения с базой прошлых сессий – жёлтым. */
.plagiarism-alert.near, .plagiarism-alert.hist { background: var(--attention-soft); border-color: var(--attention); }
.plagiarism-alert a { color: var(--danger); text-decoration-color: var(--uline-danger); }
.plagiarism-alert a:hover { color: var(--danger); text-decoration-color: var(--danger); }
.plagiarism-alert.near a, .plagiarism-alert.hist a { color: var(--attention); text-decoration-color: var(--uline-attention); }
.plagiarism-alert.hist { color: var(--attention); }
.passage {
  background: var(--attention-soft); border-left: 3px solid var(--attention);
  padding: var(--space-2) var(--space-3); margin: var(--space-1) 0;
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: var(--text-12); font-family: var(--font-mono);
  white-space: pre-wrap; word-break: break-word;
}

.img-pair {
  display: flex; gap: var(--space-3); align-items: center; flex-wrap: wrap;
  margin: var(--space-2) 0; padding: var(--space-3); background: var(--sunken);
  border: 1px solid var(--rule); border-radius: var(--radius);
}
.img-pair img {
  max-width: 200px; max-height: 150px; object-fit: contain;
  border: 1px solid var(--rule); border-radius: var(--radius-sm); background: var(--card);
}
.img-info { font-size: var(--text-12); color: var(--muted); margin-top: var(--space-1); }
.img-blank {
  width: 120px; height: 80px; display: flex; align-items: center; justify-content: center;
  background: var(--card); border: 1px solid var(--rule); border-radius: var(--radius-sm);
  color: var(--muted-soft); font-size: var(--text-12);
}

/* Кнопки повторяют интерфейс: заливка брендом и контурная. */
.print-btn {
  display: inline-flex; align-items: center; gap: var(--space-2);
  padding: var(--space-2) var(--space-4); background: var(--brand); color: var(--on-brand);
  border: 1px solid var(--brand); border-radius: var(--radius);
  font-size: var(--text-13); font-weight: 700; cursor: pointer;
  transition: background .15s ease, border-color .15s ease;
}
.print-btn:hover { background: var(--brand-hover); border-color: var(--brand-hover); color: var(--on-brand); }
.report-toolbar {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap;
  gap: var(--space-3); margin-bottom: var(--space-5); padding-bottom: var(--space-4);
  border-bottom: 1px solid var(--rule);
}
.toolbar-actions { display: flex; align-items: center; gap: var(--space-2); }
/* Переключатель темы – контурная иконка, как в топбаре интерфейса. */
.theme-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 34px; height: 34px; padding: 0;
  background: var(--card); color: var(--muted);
  border: 1px solid var(--rule); border-radius: var(--radius); cursor: pointer;
  transition: background .15s ease, border-color .15s ease, color .15s ease;
}
.theme-btn:hover { background: var(--sunken); color: var(--ink); border-color: var(--rule-strong); }
.theme-btn svg { width: 17px; height: 17px; display: block; }

@media print {
  /* Бумага и PDF – всегда светлые: отчёт здесь документ, а не экран.
     Селектор повторяет :root[data-theme="dark"] – иначе тот перебил бы
     этот блок по специфичности, и печать из тёмной темы шла бы тёмной. */
  :root, :root[data-theme="dark"] {
    color-scheme: light;
    --brand:#015D1E; --brand-hover:#024E19; --on-brand:#FFFFFF;
    --ink:#1A1F1B; --ink-strong:#0B0F0C; --muted:#5F6B62; --muted-soft:#8C968F;
    --surface:#FFFFFF; --surface-2:#f6f8f6; --surface-3:#edf1ee;
    --card:#FFFFFF; --sunken:#f6f8f6;
    --rule:#D9DFDB; --rule-strong:#B8C2BC;
    --success:#027A48; --success-soft:#DFF3E8;
    --attention:#B8860B; --attention-soft:#FAF1DC;
    --danger:#B42318; --danger-soft:#FEE4E2;
    --info:#1F5FA8; --info-soft:#E8EFF8;
    --rail:#04361a; --rail-ink:#cfe3d6; --rail-strong:#b7d6c2;
    --rail-muted:#6f9c81; --rail-rule:rgba(255,255,255,.12);
    --uline:rgba(1,93,30,.4);
    --uline-danger:rgba(180,35,24,.4);
    --uline-attention:rgba(184,134,11,.4);
  }
  body { background: #fff; color: var(--ink); font-size: 11px; }
  .print-btn, .theme-btn, .toggle-arrow { display: none !important; }
  /* Тёмная плашка шапки на бумаге только съедает тонер. */
  .brand-head { background: #fff; color: var(--ink); border: 1px solid var(--rule); }
  .brand-head h1 { color: var(--ink-strong); }
  .brand-head .subtitle, .brand-acronym, .brand-team { color: var(--muted); }
  .brand-acronym { border-left-color: var(--rule); }
  .brand-acronym b, .brand-team b { color: var(--ink-strong); }
  .brand-head, .brand-foot { break-inside: avoid; }
  /* На печати grid раскладывается в столбик и съедает страницу – плитки
     ставим потоком. */
  .stats { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }
  .stat-card { flex: 1 1 150px; padding: 12px 14px; }
  .stat-num { font-size: 1.6rem; }
  .report-body, .section-body { display: block !important; }
  .section-head { cursor: default; }
  .report-header { cursor: default; }
  .report-card, .section { break-inside: avoid; }
  .section { padding: 14px 16px; }
  .matrix-scroll { overflow: visible; }
  /* Две колонки карточки – таблицей, а не grid: WeasyPrint не сжимает
     grid-колонку до её доли, и правая половина уходила за поле страницы.
     table-layout:fixed делит полосу набора пополам независимо от того,
     что внутри. */
  .grid-2 {
    display: table; width: 100%; table-layout: fixed;
    border-collapse: separate; border-spacing: 0;
  }
  .grid-2 > div { display: table-cell; width: 50%; vertical-align: top; }
  .grid-2 > div:first-child { padding-right: 16px; }
  /* Шапка карточки: flex с nowrap-строкой «ГОСТ / Схожесть» на печати
     тоже вылезала вправо. Обычный блок переносит её по словам. */
  .report-header { display: block; padding: 8px 12px; }
  .report-header > span { white-space: normal; }
  .passage { white-space: pre-wrap; }
  a { color: inherit; text-decoration: none; }
  /* Поля страницы задаёт @page; собственный отступ контейнера сверх них
     сужал полосу набора – матрица считает ширину по чистым 170 мм. */
  .container { padding: 0; }

  /* Сводная таблица. На экране она прокручивается внутри .tbl-wrap, в PDF
     прокрутки нет: пять колонок с nowrap-шапкой и длинными ФИО уезжали за
     правое поле. Ширины задаются явно, содержимое переносится. */
  .tbl-wrap { overflow: visible; }
  .section.summary { break-inside: auto; }
  .summary-table { table-layout: fixed; width: 100%; font-size: 9px; }
  .summary-table thead { display: table-header-group; }   /* шапка на каждой странице */
  .summary-table th, .summary-table td {
    white-space: normal; overflow-wrap: break-word;
    padding: 4px 6px; vertical-align: top;
  }
  .summary-table th { font-size: 7.5px; letter-spacing: .04em; }
  .summary-table th:nth-child(1), .summary-table td:nth-child(1) { width: 26%; }
  .summary-table th:nth-child(2), .summary-table td:nth-child(2) { width: 30%; }
  .summary-table th:nth-child(3), .summary-table td:nth-child(3) { width: 15%; }
  .summary-table th:nth-child(4), .summary-table td:nth-child(4) { width: 11%; }
  .summary-table th:nth-child(5), .summary-table td:nth-child(5) { width: 18%; }
  .summary-table .badge { white-space: normal; padding: 1px 5px; }
  .report-toolbar { border-bottom: 0; padding-bottom: 0; margin-bottom: 14px; }
}

/* Рекомендуемая оценка и готовый отзыв */
.verdict {
  border: 1px solid var(--rule); border-left: 4px solid var(--brand);
  border-radius: var(--radius); padding: var(--space-3) var(--space-4);
  margin-bottom: var(--space-4); background: var(--sunken); break-inside: avoid;
}
.verdict-head { display: flex; align-items: center; gap: var(--space-3); margin-bottom: var(--space-2); }
.verdict-head h3 { font-size: var(--text-14); }
.verdict-grade { font-size: var(--text-18); font-weight: 700; font-variant-numeric: tabular-nums; }
.copy-btn {
  margin-left: auto; font: inherit; font-size: var(--text-12); font-weight: 600; cursor: pointer;
  border: 1px solid var(--rule); background: var(--card); color: var(--ink);
  border-radius: var(--radius); padding: var(--space-1) var(--space-3);
  transition: background .15s ease, border-color .15s ease, color .15s ease;
}
.copy-btn:hover { background: var(--brand); border-color: var(--brand); color: var(--on-brand); }
.flaw-list { margin: 0; padding-left: var(--space-5); font-size: var(--text-14); line-height: var(--leading-base); }
.flaw-list li { margin-bottom: 2px; }
.flaw-note { margin: var(--space-2) 0 0; font-size: var(--text-12); color: var(--muted); }
@media print { .copy-btn { display: none; } }
''' + logo_css

    js = '''
/* Переключатель темы. Выбор пишется в тот же ключ, что и в интерфейсе,
   поэтому переключение в отчёте переносится на приложение и обратно.
   Скачанный файл открывают с диска – там свой localStorage, кнопка всё
   равно работает, просто ни с чем не синхронизируется. */
(function () {
  var btn = document.getElementById('theme-btn');
  if (!btn) return;
  btn.addEventListener('click', function () {
    var root = document.documentElement;
    var dark = root.dataset.theme
      ? root.dataset.theme === 'dark'
      : window.matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('alena-theme', root.dataset.theme); } catch (e) {}
  });
})();

document.querySelectorAll('.report-header, .section-head').forEach(function(h) {
  h.addEventListener('click', function() {
    var body = this.nextElementSibling;
    var arrow = this.querySelector('.toggle-arrow');
    body.classList.toggle('open');
    if (arrow) arrow.classList.toggle('open');
  });
});

/* Копирование отзыва. Кнопка живёт внутри раскрывающейся карточки –
   клик не должен её сворачивать, поэтому всплытие останавливаем. */
document.querySelectorAll('.copy-btn').forEach(function(btn) {
  btn.addEventListener('click', function(e) {
    e.stopPropagation();
    var text = btn.dataset.text || '';
    var done = function() {
      var was = btn.textContent;
      btn.textContent = 'Скопировано';
      setTimeout(function() { btn.textContent = was; }, 1600);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done);
      return;
    }
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (err) {}
    document.body.removeChild(ta);
  });
});
'''

    return f'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(page_title)}</title>
<script>/* Тема ставится до отрисовки, иначе тёмный отчёт мигал бы белым.
Ключ тот же, что в интерфейсе: отчёт отдаётся с того же домена, и выбор,
сделанный в топбаре, действует и здесь. */
(function () {{
  try {{
    var t = localStorage.getItem('alena-theme');
    if (t === 'dark' || t === 'light') document.documentElement.dataset.theme = t;
  }} catch (e) {{}}
}})();</script>
<style>{css}</style>
</head>
<body>
<div class="container">

<div class="brand-head">
  {logo_img}
  <div>
    <h1>{branding.APP_TITLE}</h1>
    <p class="subtitle">{branding.APP_TAGLINE} · ГОСТ 7.32-2017</p>
    <p class="brand-team"><b>{branding.TEAM}</b></p>
  </div>
  <ul class="brand-acronym">{acronym}</ul>
</div>

<div class="report-toolbar">
  <div>
    <p class="subtitle" style="margin:0;">
      Сформировано: {now} &nbsp;|&nbsp;
      Отчётов: {n} &nbsp;|&nbsp;
      Порог заимствования: {thr_pct}%
    </p>
  </div>
  <div class="toolbar-actions">
    <button type="button" class="theme-btn" id="theme-btn"
            aria-label="Сменить тему" title="Светлая / тёмная тема">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"/></svg>
    </button>
    {dl_btn}
  </div>
</div>

<div class="stats">
  <div class="stat-card c-blue">
    <div class="stat-num">{n}</div>
    <div class="stat-lbl">Отчётов проверено</div>
  </div>
  <div class="stat-card {'c-red' if flagged_text else 'c-green'}">
    <div class="stat-num">{flagged_text}</div>
    <div class="stat-lbl">С заимствованием текста (&gt;{thr_pct}%)</div>
  </div>
  <div class="stat-card {'c-red' if flagged_img else 'c-green'}">
    <div class="stat-num">{flagged_img}</div>
    <div class="stat-lbl">С дубликатами изображений</div>
  </div>
  <div class="stat-card {'c-green' if gost_full == n else 'c-amber'}">
    <div class="stat-num">{gost_full}/{n}</div>
    <div class="stat-lbl">Полностью соответствуют ГОСТ</div>
  </div>{cross_card}
</div>

<div class="section summary">
  <h2>Сводная таблица</h2>
  <div class="tbl-wrap">{summary_table}</div>
</div>

<div class="section">
  <h2>Матрица схожести текстов</h2>
  <p class="subtitle" style="margin-bottom:var(--space-3);">
    Жаккар по 5-граммам слов. Красный – выше порога {thr_pct}%, жёлтый – {int(thr_pct*0.55)}–{thr_pct}%.
    {'Включены совпадения с предыдущими сессиями (выделены желтоватым фоном).' if historical_relevant else ''}
  </p>
  {matrix_html}
</div>

<h2 style="margin-bottom:var(--space-2);">Детальный анализ по каждому отчёту</h2>
<p class="subtitle" style="margin-bottom:var(--space-3);">
  Нажмите на карточку, чтобы раскрыть подробности.
</p>
{cards}

{img_summary}

<div class="brand-foot">
  {'<span class="logo-mark"></span>' if logo else ''}
  <div>
    <b>{branding.APP_TITLE}</b> – {branding.APP_FULL_NAME}.<br>
    {branding.TEAM} · версия {branding.APP_VERSION}
  </div>
  <div class="right">
    Сформировано: {now}
  </div>
</div>

</div>
<script>{js}</script>
</body>
</html>'''
