"""HTML report generation with support for historical (memory) reports."""
import re
import html as _html
from pathlib import Path
from datetime import datetime

from . import branding, grading


def _esc(s: str) -> str:
    return _html.escape(str(s))


def _cell_inline_style(sim: float, threshold: float) -> str:
    """Compute inline background for matrix cell, works in PDF (no JS needed)."""
    if sim >= threshold:
        intensity = min(1.0, 0.3 + (sim - threshold) / max(1 - threshold, 0.001) * 0.7)
        text_color = 'white' if intensity > 0.55 else '#101c14'
        return (f'background:rgba(179,38,30,{intensity:.2f});'
                f'color:{text_color};font-weight:600;')
    if sim >= threshold * 0.55:
        intensity = (sim - threshold * 0.55) / (threshold * 0.45 + 0.001) * 0.45
        return f'background:rgba(208,135,0,{intensity:.2f});'
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


def _anchor(report: dict) -> str:
    if report.get('is_historical'):
        return ''   # historical reports have no card
    return 'r_' + re.sub(r'[^a-zA-Z0-9]', '_', Path(report['path']).stem)[:30]


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
    all_reports = new_reports + historical_relevant
    if not matrix or len(all_reports) < 2:
        return '<p style="color:#8ba394">Недостаточно отчётов для матрицы.</p>'

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
            return ' style="background:#fbf0d8;color:#7a4a12;"'
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
        row_th_style = ' style="background:#fbf6e6;color:#7a4a12;"' if is_h1 else ''
        name1 = _esc(_short_name(report_by_path[p1]))
        cells = []
        for p2 in paths:
            if p1 == p2:
                cells.append('<td class="mc cell-self">·</td>')
            elif p1 in hist_paths and p2 in hist_paths:
                cells.append('<td class="mc cell-self" style="color:#c2d2c6;">·</td>')
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
            f'<span style="display:inline-block;width:14px;height:14px;'
            f'background:#fbf0d8;border:1px solid #e3c169;border-radius:2px;vertical-align:middle;"></span>'
            f' Строки/столбцы на жёлтом — отчёты из базы предыдущих сессий &nbsp;'
        )

    return f'''{dims}
<div class="matrix-scroll">
<table class="matrix-table">
  <thead><tr><th class="corner"></th>{headers}</tr></thead>
  <tbody>{"".join(rows)}</tbody>
</table>
</div>
<p style="font-size:0.78rem;color:#8ba394;margin-top:8px;">
  <span style="display:inline-block;width:14px;height:14px;background:#eab3ae;border-radius:2px;vertical-align:middle;"></span> ≥{threshold_pct}% — заимствование &nbsp;
  <span style="display:inline-block;width:14px;height:14px;background:#f0dba6;border-radius:2px;vertical-align:middle;"></span> {int(threshold_pct*0.55)}–{threshold_pct}% — близко &nbsp;
  {hist_note}
</p>'''


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
        body = ('<p style="color:#17805a;font-weight:600;">'
                '✓ Одинаковых изображений между отчётами не найдено.</p>')
        return f'''<div class="section">
  <div class="section-head">
    <h2 style="margin:0;">Дублирование изображений {' '.join(head_badges)}</h2>
    <span class="toggle-arrow">▼</span>
  </div>
  <div class="section-body">{body}</div>
</div>'''

    items = []
    for p in pairs:
        r1 = report_by_path.get(p['report1'], {'path': p['report1']})
        r2 = report_by_path.get(p['report2'], {'path': p['report2']})
        n1 = _esc(_display_name(r1))
        n2 = _esc(_display_name(r2))

        def _name_with_badge(rep, name):
            if rep.get('is_historical'):
                v = rep.get('historical_version', '?')
                d = rep.get('historical_date', '')
                return (f'{name} <span style="background:#fbf0d8;color:#7a4a12;'
                        f'padding:1px 6px;border-radius:3px;font-size:0.73rem;">'
                        f'база v{v}</span>')
            return name

        n1_html = _name_with_badge(r1, n1)
        n2_html = _name_with_badge(r2, n2)

        if p.get('ui_review'):
            match_badge = '<span style="background:#dfe9f5;color:#245a9c;padding:2px 8px;border-radius:4px;font-size:0.76rem;font-weight:600;">похожий интерфейс — проверьте вручную</span>'
        elif p.get('is_crop'):
            match_badge = '<span style="background:#fbf0d8;color:#8a5200;padding:2px 8px;border-radius:4px;font-size:0.76rem;font-weight:600;">обрезанная копия</span>'
        else:
            match_badge = '<span style="background:#fae4e2;color:#b3261e;padding:2px 8px;border-radius:4px;font-size:0.76rem;font-weight:600;">точная копия</span>'

        img1_html = (f'<img src="{p["img1"]}" alt="img1">' if p.get('img1')
                     else '<div style="width:120px;height:80px;background:#f6f9f4;border:1px solid #dbe4dc;border-radius:4px;display:flex;align-items:center;justify-content:center;color:#8ba394;font-size:0.75rem;">нет превью</div>')
        img2_html = (f'<img src="{p["img2"]}" alt="img2">' if p.get('img2')
                     else '<div style="width:120px;height:80px;background:#f6f9f4;border:1px solid #dbe4dc;border-radius:4px;display:flex;align-items:center;justify-content:center;color:#8ba394;font-size:0.75rem;">нет превью</div>')

        items.append(f'''
<div class="img-pair">
  <div>
    {img1_html}
    <div class="img-info">{n1_html}<br>стр. {p["page1"]}</div>
  </div>
  <div style="align-self:center;font-size:1.5rem;color:#b3261e;">≈</div>
  <div>
    {img2_html}
    <div class="img-info">{n2_html}<br>стр. {p["page2"]}</div>
  </div>
  <div class="img-info" style="align-self:center;">
    {match_badge}<br>
    <span style="color:#8ba394;font-size:0.75rem;">расст. {p["distance"]}/144</span>
  </div>
</div>''')

    review_note = ''
    if review:
        review_note = ('<p style="color:#64786a;font-size:0.82rem;margin:4px 0 10px;">'
                       'Пары «похожий интерфейс» — это скриншоты одинаковых программ '
                       '(терминал, Zabbix и т.п.): совпадение оформления ожидаемо, '
                       'в статистику заимствований они не входят.</p>')

    return f'''<div class="section">
  <div class="section-head">
    <h2 style="margin:0;">Дублирование изображений {' '.join(head_badges)}</h2>
    <span class="toggle-arrow">▼</span>
  </div>
  <div class="section-body">
  {review_note}
  {''.join(items)}
  </div>
</div>'''


def _render_gost_table(gost_results: list) -> str:
    rows = []
    for c in gost_results:
        if c['passed']:
            icon = '<span class="check-pass">✓</span>'
        elif c['severity'] == 'warning':
            icon = '<span class="check-warn">⚠</span>'
        else:
            icon = '<span class="check-fail">✗</span>'
        details = (f'<span style="color:#64786a;font-size:0.82rem;">{_esc(c["details"])}</span>'
                   if c['details'] else '')
        rows.append(
            f'<tr><td>{icon}</td><td><b>{_esc(c["name"])}</b></td>'
            f'<td>{details}</td></tr>'
        )
    return (
        '<table class="checks-table">'
        '<thead><tr><th style="width:28px"></th><th>Проверка</th><th>Подробности</th></tr></thead>'
        '<tbody>' + ''.join(rows) + '</tbody></table>'
    )


def _render_text_plag_for_report(path: str, text_plagiarism: dict, threshold: float,
                                  report_by_path: dict) -> str:
    matrix = text_plagiarism.get('matrix', {})
    pairs  = text_plagiarism.get('pairs', [])

    sims = [
        (other, sim)
        for other, sim in matrix.get(path, {}).items()
        if other != path
    ]
    sims.sort(key=lambda x: -x[1])

    if not sims:
        return '<p style="color:#8ba394;font-size:0.85rem;">Нет данных.</p>'

    max_other, max_sim = sims[0]

    if max_sim < threshold * 0.3:
        return (
            '<p style="color:#17805a;font-weight:600;font-size:0.9rem;">'
            f'✓ Заимствования не обнаружено (макс. схожесть {max_sim:.0%})</p>'
        )

    parts = []
    for other_path, sim in sims[:5]:
        if sim < threshold * 0.3:
            continue
        other_rep   = report_by_path.get(other_path, {'path': other_path})
        other_name  = _esc(_display_name(other_rep))
        is_hist     = other_rep.get('is_historical', False)

        if is_hist:
            hist_ver  = other_rep.get('historical_version', '?')
            hist_date = other_rep.get('historical_date', '')
            badge_cls = 'badge-red' if sim >= threshold else 'badge-amber'
            label     = 'ИЗ БАЗЫ' if sim >= threshold else 'База (близко)'
            ref_html  = (
                f'<span style="color:#7a4a12;">{other_name}</span> '
                f'<span style="color:#8a5200;font-size:0.78rem;">'
                f'(база v{hist_ver}, {hist_date})</span>'
            )
            alert_style = 'background:#fdf8ea;border-color:#f0dba6;'
        else:
            anchor    = _anchor(other_rep)
            badge_cls = 'badge-red' if sim >= threshold else 'badge-amber'
            label     = 'ЗАИМСТВОВАНИЕ' if sim >= threshold else 'Близко'
            ref_html  = f'<a href="#{anchor}" style="color:#8f1d17;">{other_name}</a>'
            alert_style = ''

        passages_html = ''
        for pair in pairs:
            if {pair['report1'], pair['report2']} == {path, other_path}:
                if pair['passages']:
                    items = ''.join(
                        f'<div class="passage">{_esc(p[:280])}…</div>'
                        for p in pair['passages'][:3]
                    )
                    passages_html = f'<div style="margin-top:8px;">{items}</div>'
                break

        parts.append(
            f'<div class="plagiarism-alert" style="margin-bottom:8px;{alert_style}">'
            f'<span class="badge {badge_cls}">{label}</span> '
            f'<strong>{sim:.0%}</strong> совпадений с '
            f'{ref_html}'
            f'{passages_html}</div>'
        )

    return ''.join(parts) if parts else (
        '<p style="color:#d08700;font-size:0.85rem;">'
        f'Схожесть до {max_sim:.0%}, ниже порога {threshold:.0%}</p>'
    )


def _render_img_plag_for_report(path: str, image_plagiarism: dict,
                                 report_by_path: dict) -> str:
    my_pairs = [
        p for p in image_plagiarism.get('pairs', [])
        if p['report1'] == path or p['report2'] == path
    ]
    if not my_pairs:
        return '<p style="color:#17805a;font-weight:600;font-size:0.9rem;">✓ Дублей изображений нет</p>'

    confirmed = [p for p in my_pairs if not p.get('ui_review')]
    review    = [p for p in my_pairs if p.get('ui_review')]

    items = []
    for p in my_pairs:
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
                f'<span style="background:#fbf0d8;color:#7a4a12;'
                f'padding:1px 5px;border-radius:3px;font-size:0.72rem;">база v{v}</span>'
                f'<br>стр.{other_page}'
            )
        else:
            other_label = f'{other_name}, стр.{other_page}'

        other_img_html = (
            f'<img src="{other_img}" alt="other" style="max-height:100px;">'
            if other_img else
            '<div style="width:100px;height:70px;background:#f6f9f4;border:1px solid #dbe4dc;'
            'display:flex;align-items:center;justify-content:center;color:#8ba394;font-size:0.72rem;">нет превью</div>'
        )

        review_badge = ''
        if p.get('ui_review'):
            review_badge = (
                '<div style="align-self:center;">'
                '<span class="badge badge-blue">похожий интерфейс — проверьте вручную</span>'
                '</div>'
            )

        items.append(
            f'<div class="img-pair" style="margin:6px 0;">'
            f'<div><img src="{my_img}" alt="my" style="max-height:100px;">'
            f'<div class="img-info">Эта работа, стр.{my_page}</div></div>'
            f'<div style="align-self:center;color:#b3261e;font-size:1.3rem;">≈</div>'
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
    return ' '.join(badges) + ''.join(items)


def _render_feedback(report: dict, gost_results: list, max_sim: float,
                     threshold: float, weights: dict, scale: int) -> str:
    """Рекомендуемая оценка и готовый к копированию отзыв для одной работы."""
    mark = grading.grade(gost_results, weights, scale)
    student = {
        'fio':   _display_name(report),
        'group': (report.get('student') or {}).get('group', ''),
        'flaws': grading.flaws(gost_results),
        'plag':  round(max_sim * 100),
        'grade': mark,
    }
    thr_pct = int(threshold * 100)
    lines = grading.feedback_lines(student, thr_pct)
    plain = grading.feedback_text(student, thr_pct)

    pct = mark['pct'] or 0
    color = '#17805a' if pct >= 85 else '#d08700' if pct >= 60 else '#b3261e'
    in_points = (f' &nbsp;<span style="font-size:0.8rem;color:#64786a;">'
                 f'{mark["score"]:g} из {mark["scale"]}</span>'
                 if mark['score'] is not None else '')

    if lines:
        items = ''.join(f'<li>{_esc(l)}</li>' for l in lines)
        body = f'<ul class="flaw-list">{items}</ul>'
    else:
        body = ('<p style="color:#17805a;font-size:0.85rem;margin:0;">'
                'Замечаний по оформлению нет.</p>')

    costly = ''
    if mark['lost']:
        top = mark['lost'][0]
        costly = (f'<p class="flaw-note">Дороже всего обошлось: '
                  f'{_esc(top["name"])} — минус {top["weight"]:g}%.</p>')

    return f'''
    <div class="verdict">
      <div class="verdict-head">
        <h3 style="margin:0;">Рекомендуемая оценка за оформление</h3>
        <span class="verdict-grade" style="color:{color};">{pct}%</span>{in_points}
        <button type="button" class="copy-btn" data-text="{_esc(plain)}">Копировать отзыв</button>
      </div>
      {body}
      {costly}
    </div>'''


def _render_card(report: dict, text_plagiarism: dict, image_plagiarism: dict,
                 threshold: float, report_by_path: dict,
                 weights: dict = None, scale: int = grading.DEFAULT_SCALE) -> str:
    path = report['path']
    gost_results = report.get('gost_results', [])
    passed, total = _gost_score(gost_results)
    score_pct = int(passed / total * 100) if total else 0

    matrix = text_plagiarism.get('matrix', {})
    max_sim, _ = _max_sim(path, matrix)

    has_text_plag = max_sim >= threshold
    has_img_plag  = any(
        (p['report1'] == path or p['report2'] == path) and not p.get('ui_review')
        for p in image_plagiarism.get('pairs', [])
    )

    if has_text_plag or has_img_plag:
        badge = '<span class="badge badge-red">Заимствование</span>'
        header_border = 'border-left:4px solid #b3261e;'
    elif passed < total * 0.7:
        badge = '<span class="badge badge-amber">Нарушения ГОСТ</span>'
        header_border = 'border-left:4px solid #d08700;'
    else:
        badge = '<span class="badge badge-green">OK</span>'
        header_border = 'border-left:4px solid #17805a;'

    score_color = ('#17805a' if score_pct >= 85 else
                   '#d08700' if score_pct >= 60 else '#b3261e')

    s = report.get('student', {})
    meta_parts = []
    if s.get('group'):      meta_parts.append(f'Группа: <b>{_esc(s["group"])}</b>')
    if s.get('year'):       meta_parts.append(f'Год: {_esc(s["year"])}')
    if s.get('work_title'): meta_parts.append(_esc(s['work_title'][:80]))
    meta = ' &nbsp;|&nbsp; '.join(meta_parts)

    scan_warn = ''
    if report.get('is_scanned'):
        scan_warn = ('<div class="badge badge-amber" style="margin-bottom:12px;">'
                     '⚠ Возможно, отсканированный PDF, текст не извлечён</div>')

    fname = report.get('filename', '') or Path(path).name
    fname_disp = _esc(fname)

    if report.get('error'):
        return f'''
<div class="report-card" id="{_anchor(report)}">
  <div class="report-header" style="{header_border}">
    <span style="font-size:1rem;font-weight:600;flex:1;">{_esc(_display_name(report))}</span>
    <span class="badge badge-red">Ошибка чтения</span>
  </div>
  <div class="report-body">
    <p style="color:#b3261e;">{_esc(report["error"])}</p>
    <p style="color:#64786a;font-size:0.8rem;">{fname_disp}</p>
  </div>
</div>'''

    gost_table = _render_gost_table(gost_results)
    text_plag  = _render_text_plag_for_report(path, text_plagiarism, threshold, report_by_path)
    img_plag   = _render_img_plag_for_report(path, image_plagiarism, report_by_path)

    return f'''
<div class="report-card" id="{_anchor(report)}">
  <div class="report-header" style="{header_border}">
    <span style="font-size:1rem;font-weight:600;flex:1;">{_esc(_display_name(report))}</span>
    {badge}
    <span style="font-size:0.82rem;color:#64786a;white-space:nowrap;">
      ГОСТ: {passed}/{total} &nbsp;|&nbsp; Схожесть: {max_sim:.0%}
    </span>
    <span class="toggle-arrow">▼</span>
  </div>
  <div class="report-body">
    {scan_warn}
    {f'<p style="color:#64786a;font-size:0.84rem;margin-bottom:10px;">{meta}</p>' if meta else ''}
    <p style="font-size:0.76rem;color:#8ba394;margin-bottom:16px;">📄 {fname_disp}</p>

    {_render_feedback(report, gost_results, max_sim, threshold, weights, scale)}

    <div class="grid-2">
      <!-- GOST -->
      <div>
        <h3>ГОСТ 7.32-2017</h3>
        <div class="score-bar" style="margin-bottom:10px;">
          <span style="font-size:0.82rem;color:#64786a;width:32px;">{score_pct}%</span>
          <div class="score-track">
            <div class="score-fill" style="width:{score_pct}%;background:{score_color};"></div>
          </div>
          <span style="font-size:0.82rem;color:#64786a;">{passed}/{total}</span>
        </div>
        {gost_table}
      </div>

      <!-- Plagiarism -->
      <div>
        <h3>Заимствование текста</h3>
        {text_plag}
        <div style="margin-top:18px;">
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
        scale: шкала оценки (100 — проценты)
    """
    now      = datetime.now().strftime('%d.%m.%Y %H:%M')
    n        = len(reports)
    thr_pct  = int(threshold * 100)

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
        if any(matrix.get(hp, {}).get(np, 0.0) > threshold * 0.25 for np in new_paths):
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
    gost_full = sum(
        1 for r in reports if all(c['passed'] for c in r.get('gost_results', []))
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
                     report_by_path, weights, scale)
        for r in reports
    )


    def _row_class(r):
        path = r['path']
        sim, _ = _max_sim(path, matrix)
        has_plag = sim >= threshold or any(
            p['report1'] == path or p['report2'] == path for p in img_confirmed
        )
        p, t = _gost_score(r.get('gost_results', []))
        if has_plag:       return 'tr-red'
        if p < t * 0.7:   return 'tr-amber'
        return 'tr-green'

    summary_rows = []
    for r in reports:
        path = r['path']
        sim, sim_other = _max_sim(path, matrix)

        other_rep = report_by_path.get(sim_other) if sim_other else None
        if other_rep and other_rep.get('is_historical'):
            v = other_rep.get('historical_version', '?')
            d = other_rep.get('historical_date', '')
            other_name_html = (
                f'{_esc(_display_name(other_rep))} '
                f'<span style="color:#b26a00;font-size:0.75rem;">(база v{v}, {d})</span>'
            )
        elif other_rep:
            anc = _anchor(other_rep)
            other_name_html = f'<a href="#{anc}">{_esc(_display_name(other_rep))}</a>'
        else:
            other_name_html = '—'

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
        plag_badge = (f'<span class="badge badge-red">{sim:.0%}</span>'
                      if sim >= threshold else
                      f'<span style="color:#64786a;">{sim:.0%}</span>')
        gost_badge = (
            f'<span class="badge badge-green">{p}/{t}</span>' if p == t else
            f'<span class="badge {"badge-amber" if p >= t * 0.7 else "badge-red"}">{p}/{t}</span>'
        )
        if img_count:
            img_badge = f'<span class="badge badge-red">{img_count} дублей</span>'
        elif review_count:
            img_badge = f'<span class="badge badge-blue">{review_count} на проверку</span>'
        else:
            img_badge = '<span style="color:#17805a;">—</span>'
        mark = grading.grade(r.get('gost_results', []), weights, scale)
        mark_pct = mark['pct'] or 0
        mark_text = (f'{mark["score"]:g} из {mark["scale"]}'
                     if mark['score'] is not None else f'{mark_pct}%')
        mark_badge = (
            f'<span class="badge badge-green">{mark_text}</span>' if mark_pct >= 85 else
            f'<span class="badge {"badge-amber" if mark_pct >= 60 else "badge-red"}">'
            f'{mark_text}</span>'
        )
        anchor = _anchor(r)
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


    # Логотип встраивается один раз — фоном для двух меток, иначе строка
    # base64 весом под 70 КБ лежала бы в файле дважды.
    logo = branding.logo_data_uri()
    logo_css = f'.logo-mark {{ background-image: url({logo}); }}' if logo else ''
    logo_img = '<span class="brand-logo logo-mark"></span>' if logo else ''
    acronym = ''.join(f'<li><b>{letter}</b>{word[1:]}</li>'
                      for letter, word in branding.ACRONYM)

    page_rule = f'''@page {{
  size: A4 portrait;
  margin: 15mm 20mm 24mm 20mm;
  @bottom-left {{
    content: "{branding.TEAM} · {branding.APP_TITLE} — {branding.APP_FULL_NAME}";
    font-size: 7.5pt;
    color: #8ba394;
    font-family: Arial, sans-serif;
  }}
  @bottom-right {{
    content: "Сформировано: {now}  ·  стр. " counter(page) " / " counter(pages);
    font-size: 7.5pt;
    color: #8ba394;
    font-family: Arial, sans-serif;
  }}
}}'''

    css = page_rule + '''
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', Arial, sans-serif; background: #eef2ec; color: #101c14; line-height: 1.5; font-size: 14px; }
.container { max-width: 1400px; margin: 0 auto; padding: 24px; }
h1 { font-size: 1.5rem; font-weight: 700; letter-spacing: -.01em; }
h2 { font-size: 1.15rem; font-weight: 600; margin: 0 0 12px; }
h3 { font-size: 0.95rem; font-weight: 600; margin-bottom: 8px; }
.subtitle { color: #64786a; font-size: 0.88rem; margin: 6px 0 0; }
a { color: #0a7333; text-decoration: none; }
a:hover { text-decoration: underline; }

/* Шапка и подвал: та же марка, что и в интерфейсе — логотип #au_team,
   название и расшифровка по буквам. */
.brand-head {
  display: flex; align-items: flex-start; gap: 16px; flex-wrap: wrap;
  background: #04361a; color: #cfe3d6; border-radius: 12px;
  padding: 18px 22px; margin-bottom: 18px;
}
.logo-mark {
  background-color: #fff; background-repeat: no-repeat; background-position: center;
  background-size: contain; background-origin: content-box; display: inline-block;
}
.brand-logo { width: 54px; height: 54px; flex: none; border-radius: 10px; padding: 4px; }
.brand-head h1 { color: #fff; }
.brand-head .subtitle { color: #8fb69f; }
.brand-acronym { list-style: none; margin: 0 0 0 auto; padding: 0 0 0 18px; font-size: 0.76rem; line-height: 1.5; color: #8fb69f; border-left: 1px solid rgba(255,255,255,.12); }
.brand-acronym b { color: #d8ebdf; font-weight: 800; }
.brand-team { font-size: 0.78rem; color: #8fb69f; margin-top: 8px; }
.brand-team b { color: #d8ebdf; }

.brand-foot {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin-top: 26px; padding: 14px 18px; background: white;
  border: 1px solid #dbe4dc; border-radius: 12px;
  font-size: 0.78rem; color: #64786a;
}
.brand-foot .logo-mark { width: 32px; height: 32px; flex: none; border-radius: 7px; padding: 2px; }
.brand-foot b { color: #101c14; }
.brand-foot .right { margin-left: auto; text-align: right; }

.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 28px; }
.stat-card { background: white; border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.stat-num { font-size: 2.2rem; font-weight: 700; line-height: 1; }
.stat-lbl { color: #64786a; font-size: 0.82rem; margin-top: 4px; }
.c-blue .stat-num   { color: #0a7333; }
.c-red .stat-num    { color: #b3261e; }
.c-amber .stat-num  { color: #d08700; }
.c-green .stat-num  { color: #17805a; }

.section { background: white; border-radius: 12px; padding: 22px 24px; box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 22px; }
.section-head { display: flex; align-items: center; gap: 10px; cursor: pointer; }
.section-head h2 { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.section-head .toggle-arrow { margin-left: auto; }
.section-body { display: none; margin-top: 14px; }
.section-body.open { display: block; }

.summary-table { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
.summary-table th { text-align: left; padding: 8px 12px; background: #f6f9f4; color: #64786a; font-weight: 600; border-bottom: 2px solid #dbe4dc; }
.summary-table td { padding: 8px 12px; border-bottom: 1px solid #eef2ec; vertical-align: middle; }
.tr-red   td:first-child { border-left: 3px solid #b3261e; }
.tr-amber td:first-child { border-left: 3px solid #d08700; }
.tr-green td:first-child { border-left: 3px solid #17805a; }

.badge { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 0.76rem; font-weight: 600; }
.badge-red   { background: #fae4e2; color: #b3261e; }
.badge-amber { background: #fbf0d8; color: #b26a00; }
.badge-green { background: #e3f2ec; color: #17805a; }
.badge-blue  { background: #dfe9f5; color: #245a9c; }

.matrix-scroll { overflow-x: auto; }
.matrix-table { border-collapse: collapse; table-layout: fixed; margin: 0 auto; }
.matrix-table th, .matrix-table td { border: 1px solid #dbe4dc; }
.matrix-table td.mc { text-align: center; padding: 0; height: 18px; line-height: 1.1; overflow: hidden; }
.matrix-table thead th.mh { position: relative; background: #f6f9f4; vertical-align: bottom; padding: 0; overflow: hidden; }
.matrix-table thead th.mh > span { position: absolute; bottom: 4px; left: 50%; transform-origin: left bottom; transform: rotate(-90deg); white-space: nowrap; font-weight: 600; line-height: 1; }
.matrix-table thead th.corner { background: #f6f9f4; }
.matrix-table tbody th.rh { text-align: left; font-weight: 500; background: #f6f9f4; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 2px 6px; }
.cell-self { background: #dbe4dc !important; }
.matrix-cell { }

.report-card { background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 14px; overflow: hidden; }
.report-header { padding: 14px 18px; display: flex; align-items: center; gap: 10px; cursor: pointer; }
.report-header:hover { background: #f6f9f4; }
.report-body { padding: 20px; display: none; border-top: 1px solid #eef2ec; }
.report-body.open { display: block; }
.toggle-arrow { color: #8ba394; font-size: 0.8rem; transition: transform .2s; margin-left: auto; }
.toggle-arrow.open { transform: rotate(180deg); }

.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
@media (max-width: 860px) { .grid-2 { grid-template-columns: 1fr; } }

.checks-table { width: 100%; border-collapse: collapse; font-size: 0.83rem; }
.checks-table th { text-align: left; padding: 6px 10px; background: #f6f9f4; color: #64786a; font-weight: 600; border-bottom: 1px solid #dbe4dc; }
.checks-table td { padding: 6px 10px; border-bottom: 1px solid #f6f9f4; vertical-align: top; }
.check-pass { color: #17805a; font-weight: 700; }
.check-fail { color: #b3261e; font-weight: 700; }
.check-warn { color: #b26a00; font-weight: 700; }

.score-bar { display: flex; align-items: center; gap: 8px; }
.score-track { flex: 1; height: 5px; background: #dbe4dc; border-radius: 3px; overflow: hidden; }
.score-fill { height: 100%; border-radius: 3px; }

.plagiarism-alert { background: #fdf1f0; border: 1px solid #f0cdc9; border-radius: 7px; padding: 10px 14px; margin-bottom: 8px; font-size: 0.86rem; }
.passage { background: #fbf4d4; border-left: 3px solid #d9a521; padding: 8px 12px; margin: 6px 0; border-radius: 0 5px 5px 0; font-size: 0.8rem; font-family: 'Consolas', monospace; white-space: pre-wrap; word-break: break-word; }

.img-pair { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin: 10px 0; padding: 12px; background: #fdf6ec; border-radius: 8px; border: 1px solid #ecd7b4; }
.img-pair img { max-width: 200px; max-height: 150px; object-fit: contain; border: 1px solid #dbe4dc; border-radius: 4px; background: white; }
.img-info { font-size: 0.8rem; color: #64786a; margin-top: 4px; }

.print-btn { display:inline-flex; align-items:center; gap:6px; padding:8px 18px; background:#015D1E; color:white; border:none; border-radius:7px; font-size:0.84rem; font-weight:700; cursor:pointer; text-decoration:none; }
.print-btn:hover { background:#014818; }
.report-toolbar { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; margin-bottom:20px; }

@media print {
  body { background: white; font-size: 11px; }
  .print-btn, .toggle-arrow { display: none !important; }
  /* Тёмная плашка шапки на бумаге только съедает тонер. */
  .brand-head { background: white; color: #101c14; border: 1px solid #dbe4dc; }
  .brand-head h1 { color: #101c14; }
  .brand-head .subtitle, .brand-acronym, .brand-team { color: #64786a; }
  .brand-acronym { border-left-color: #dbe4dc; }
  .brand-acronym b, .brand-team b { color: #101c14; }
  .brand-head, .brand-foot { break-inside: avoid; }
  /* На печати grid раскладывается в столбик и съедает страницу — плитки
     ставим потоком. */
  .stats { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }
  .stat-card { flex: 1 1 150px; padding: 12px 14px; }
  .stat-num { font-size: 1.6rem; }
  .report-body, .section-body { display: block !important; }
  .section-head { cursor: default; }
  .report-header { cursor: default; }
  .report-card, .section { break-inside: avoid; box-shadow: none; border: 1px solid #dbe4dc; }
  .matrix-scroll { overflow: visible; }
  .grid-2 { grid-template-columns: 1fr 1fr; }
  .passage { white-space: pre-wrap; }
  a { color: inherit; text-decoration: none; }
  .container { padding: 12px; }
}

/* Рекомендуемая оценка и готовый отзыв */
.verdict {
  border: 1px solid #dbe4dc; border-left: 4px solid #015D1E;
  border-radius: 8px; padding: 12px 14px; margin-bottom: 16px;
  background: #f6f9f4; break-inside: avoid;
}
.verdict-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.verdict-head h3 { font-size: 0.86rem; }
.verdict-grade { font-size: 1.1rem; font-weight: 700; }
.copy-btn {
  margin-left: auto; font: inherit; font-size: 0.78rem; cursor: pointer;
  border: 1px solid #c2d2c6; background: #fff; color: #101c14;
  border-radius: 6px; padding: 5px 10px;
}
.copy-btn:hover { background: #015D1E; border-color: #015D1E; color: #fff; }
.flaw-list { margin: 0; padding-left: 20px; font-size: 0.85rem; line-height: 1.55; }
.flaw-list li { margin-bottom: 2px; }
.flaw-note { margin: 8px 0 0; font-size: 0.78rem; color: #64786a; }
@media print { .copy-btn { display: none; } }
''' + logo_css

    js = '''
document.querySelectorAll('.report-header, .section-head').forEach(function(h) {
  h.addEventListener('click', function() {
    var body = this.nextElementSibling;
    var arrow = this.querySelector('.toggle-arrow');
    body.classList.toggle('open');
    if (arrow) arrow.classList.toggle('open');
  });
});

/* Копирование отзыва. Кнопка живёт внутри раскрывающейся карточки —
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
<title>{branding.APP_TITLE} — проверка отчётов, {now}</title>
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
  {dl_btn}
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

<div class="section">
  <h2>Сводная таблица</h2>
  {summary_table}
</div>

<div class="section">
  <h2>Матрица схожести текстов</h2>
  <p style="color:#64786a;font-size:0.83rem;margin-bottom:14px;">
    Жаккар по 5-граммам слов. Красный — выше порога {thr_pct}%, жёлтый — {int(thr_pct*0.55)}–{thr_pct}%.
    {'Включены совпадения с предыдущими сессиями (выделены желтоватым фоном).' if historical_relevant else ''}
  </p>
  {matrix_html}
</div>

<h2 style="margin-bottom:12px;">Детальный анализ по каждому отчёту</h2>
<p style="color:#64786a;font-size:0.83rem;margin-bottom:14px;">
  Нажмите на карточку, чтобы раскрыть подробности.
</p>
{cards}

{img_summary}

<div class="brand-foot">
  {'<span class="logo-mark"></span>' if logo else ''}
  <div>
    <b>{branding.APP_TITLE}</b> — {branding.APP_FULL_NAME}.<br>
    {branding.TEAM} · версия {branding.APP_VERSION}
  </div>
  <div class="right">
    Сформировано: {now}<br>
    {branding.REGISTRY_NOTE}
  </div>
</div>

</div>
<script>{js}</script>
</body>
</html>'''
