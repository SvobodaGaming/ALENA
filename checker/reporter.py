"""HTML report generation with support for historical (memory) reports."""
import re
import html as _html
from pathlib import Path
from datetime import datetime


def _esc(s: str) -> str:
    return _html.escape(str(s))


def _cell_inline_style(sim: float, threshold: float) -> str:
    """Compute inline background for matrix cell, works in PDF (no JS needed)."""
    if sim >= threshold:
        intensity = min(1.0, 0.3 + (sim - threshold) / max(1 - threshold, 0.001) * 0.7)
        text_color = 'white' if intensity > 0.55 else '#1e293b'
        return (f'background:rgba(239,68,68,{intensity:.2f});'
                f'color:{text_color};font-weight:600;')
    if sim >= threshold * 0.55:
        intensity = (sim - threshold * 0.55) / (threshold * 0.45 + 0.001) * 0.45
        return f'background:rgba(245,158,11,{intensity:.2f});'
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
        return '<p style="color:#94a3b8">Недостаточно отчётов для матрицы.</p>'

    threshold_pct = int(threshold * 100)
    hist_paths = {r['path'] for r in historical_relevant}
    report_by_path = {r['path']: r for r in all_reports}
    paths = [r['path'] for r in all_reports]

    def _th_style(p):
        if p in hist_paths:
            return ' style="background:#fef3c7;color:#92400e;"'
        return ''

    headers = ''.join(
        f'<th title="{_esc(_display_name(report_by_path[p]))}"'
        f'{_th_style(p)}>'
        f'{_esc(_short_name(report_by_path[p]))}</th>'
        for p in paths
    )

    rows = []
    for p1 in paths:
        is_h1 = p1 in hist_paths
        row_th_style = ' style="background:#fef9e7;color:#92400e;"' if is_h1 else ''
        name1 = _esc(_short_name(report_by_path[p1]))
        cells = []
        for p2 in paths:
            if p1 == p2:
                cells.append('<td class="cell-self">·</td>')
            elif p1 in hist_paths and p2 in hist_paths:
                cells.append('<td class="cell-self" style="color:#cbd5e1;">·</td>')
            else:
                sim = matrix.get(p1, {}).get(p2, 0.0)
                pct = int(sim * 100)
                style = _cell_inline_style(sim, threshold)
                cells.append(
                    f'<td class="matrix-cell" style="{style}">{pct}%</td>'
                )
        rows.append(f'<tr><th{row_th_style}>{name1}</th>{"".join(cells)}</tr>')

    hist_note = ''
    if historical_relevant:
        hist_note = (
            f'<span style="display:inline-block;width:14px;height:14px;'
            f'background:#fef3c7;border:1px solid #fcd34d;border-radius:2px;vertical-align:middle;"></span>'
            f' Строки/столбцы на жёлтом, отчёты из базы предыдущих сессий &nbsp;'
        )

    return f'''
<div class="matrix-scroll">
<table class="matrix-table">
  <thead><tr><th></th>{headers}</tr></thead>
  <tbody>{"".join(rows)}</tbody>
</table>
</div>
<p style="font-size:0.78rem;color:#94a3b8;margin-top:8px;">
  <span style="display:inline-block;width:14px;height:14px;background:#fca5a5;border-radius:2px;vertical-align:middle;"></span> ≥{threshold_pct}%, заимствование &nbsp;
  <span style="display:inline-block;width:14px;height:14px;background:#fde68a;border-radius:2px;vertical-align:middle;"></span> {int(threshold_pct*0.6)}-{threshold_pct}%, близко &nbsp;
  {hist_note}
</p>'''


def _render_image_summary(image_plagiarism: dict, report_by_path: dict) -> str:
    pairs = image_plagiarism.get('pairs', [])
    if not pairs:
        return '''<div class="section">
  <h2>Дублирование изображений</h2>
  <p style="color:#16a34a;font-weight:600;">✓ Одинаковых изображений между отчётами не найдено.</p>
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
                return (f'{name} <span style="background:#fef3c7;color:#92400e;'
                        f'padding:1px 6px;border-radius:3px;font-size:0.73rem;">'
                        f'база v{v}</span>')
            return name

        n1_html = _name_with_badge(r1, n1)
        n2_html = _name_with_badge(r2, n2)

        match_badge = (
            '<span style="background:#fef3c7;color:#b45309;padding:2px 8px;border-radius:4px;font-size:0.76rem;font-weight:600;">обрезанная копия</span>'
            if p.get('is_crop') else
            '<span style="background:#fee2e2;color:#dc2626;padding:2px 8px;border-radius:4px;font-size:0.76rem;font-weight:600;">точная копия</span>'
        )

        img1_html = (f'<img src="{p["img1"]}" alt="img1">' if p.get('img1')
                     else '<div style="width:120px;height:80px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:4px;display:flex;align-items:center;justify-content:center;color:#94a3b8;font-size:0.75rem;">нет превью</div>')
        img2_html = (f'<img src="{p["img2"]}" alt="img2">' if p.get('img2')
                     else '<div style="width:120px;height:80px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:4px;display:flex;align-items:center;justify-content:center;color:#94a3b8;font-size:0.75rem;">нет превью</div>')

        items.append(f'''
<div class="img-pair">
  <div>
    {img1_html}
    <div class="img-info">{n1_html}<br>стр. {p["page1"]}</div>
  </div>
  <div style="align-self:center;font-size:1.5rem;color:#ef4444;">≈</div>
  <div>
    {img2_html}
    <div class="img-info">{n2_html}<br>стр. {p["page2"]}</div>
  </div>
  <div class="img-info" style="align-self:center;">
    {match_badge}<br>
    <span style="color:#94a3b8;font-size:0.75rem;">расст. {p["distance"]}/144</span>
  </div>
</div>''')

    return f'''<div class="section">
  <h2>Дублирование изображений <span class="badge badge-red">{len(pairs)} пар</span></h2>
  {''.join(items)}
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
        details = (f'<span style="color:#64748b;font-size:0.82rem;">{_esc(c["details"])}</span>'
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
        return '<p style="color:#94a3b8;font-size:0.85rem;">Нет данных.</p>'

    max_other, max_sim = sims[0]

    if max_sim < threshold * 0.3:
        return (
            '<p style="color:#16a34a;font-weight:600;font-size:0.9rem;">'
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
                f'<span style="color:#92400e;">{other_name}</span> '
                f'<span style="color:#b45309;font-size:0.78rem;">'
                f'(база v{hist_ver}, {hist_date})</span>'
            )
            alert_style = 'background:#fffbeb;border-color:#fde68a;'
        else:
            anchor    = _anchor(other_rep)
            badge_cls = 'badge-red' if sim >= threshold else 'badge-amber'
            label     = 'ЗАИМСТВОВАНИЕ' if sim >= threshold else 'Близко'
            ref_html  = f'<a href="#{anchor}" style="color:#be123c;">{other_name}</a>'
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
        '<p style="color:#f59e0b;font-size:0.85rem;">'
        f'Схожесть до {max_sim:.0%}, ниже порога {threshold:.0%}</p>'
    )


def _render_img_plag_for_report(path: str, image_plagiarism: dict,
                                 report_by_path: dict) -> str:
    my_pairs = [
        p for p in image_plagiarism.get('pairs', [])
        if p['report1'] == path or p['report2'] == path
    ]
    if not my_pairs:
        return '<p style="color:#16a34a;font-weight:600;font-size:0.9rem;">✓ Дублей изображений нет</p>'

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
                f'<span style="background:#fef3c7;color:#92400e;'
                f'padding:1px 5px;border-radius:3px;font-size:0.72rem;">база v{v}</span>'
                f'<br>стр.{other_page}'
            )
        else:
            other_label = f'{other_name}, стр.{other_page}'

        other_img_html = (
            f'<img src="{other_img}" alt="other" style="max-height:100px;">'
            if other_img else
            '<div style="width:100px;height:70px;background:#f8fafc;border:1px solid #e2e8f0;'
            'display:flex;align-items:center;justify-content:center;color:#94a3b8;font-size:0.72rem;">нет превью</div>'
        )

        items.append(
            f'<div class="img-pair" style="margin:6px 0;">'
            f'<div><img src="{my_img}" alt="my" style="max-height:100px;">'
            f'<div class="img-info">Эта работа, стр.{my_page}</div></div>'
            f'<div style="align-self:center;color:#ef4444;font-size:1.3rem;">≈</div>'
            f'<div>{other_img_html}'
            f'<div class="img-info">{other_label}</div></div>'
            f'</div>'
        )
    return (
        f'<span class="badge badge-red">{len(my_pairs)} дублей</span>'
        + ''.join(items)
    )


def _render_card(report: dict, text_plagiarism: dict, image_plagiarism: dict,
                 threshold: float, report_by_path: dict) -> str:
    path = report['path']
    gost_results = report.get('gost_results', [])
    passed, total = _gost_score(gost_results)
    score_pct = int(passed / total * 100) if total else 0

    matrix = text_plagiarism.get('matrix', {})
    max_sim, _ = _max_sim(path, matrix)

    has_text_plag = max_sim >= threshold
    has_img_plag  = any(
        p['report1'] == path or p['report2'] == path
        for p in image_plagiarism.get('pairs', [])
    )

    if has_text_plag or has_img_plag:
        badge = '<span class="badge badge-red">Заимствование</span>'
        header_border = 'border-left:4px solid #ef4444;'
    elif passed < total * 0.7:
        badge = '<span class="badge badge-amber">Нарушения ГОСТ</span>'
        header_border = 'border-left:4px solid #f59e0b;'
    else:
        badge = '<span class="badge badge-green">OK</span>'
        header_border = 'border-left:4px solid #22c55e;'

    score_color = ('#22c55e' if score_pct >= 85 else
                   '#f59e0b' if score_pct >= 60 else '#ef4444')

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
    <p style="color:#ef4444;">{_esc(report["error"])}</p>
    <p style="color:#64748b;font-size:0.8rem;">{fname_disp}</p>
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
    <span style="font-size:0.82rem;color:#64748b;white-space:nowrap;">
      ГОСТ: {passed}/{total} &nbsp;|&nbsp; Схожесть: {max_sim:.0%}
    </span>
    <span class="toggle-arrow">▼</span>
  </div>
  <div class="report-body">
    {scan_warn}
    {f'<p style="color:#64748b;font-size:0.84rem;margin-bottom:10px;">{meta}</p>' if meta else ''}
    <p style="font-size:0.76rem;color:#94a3b8;margin-bottom:16px;">📄 {fname_disp}</p>

    <div class="grid-2">
      <!-- GOST -->
      <div>
        <h3>ГОСТ 7.32-2017</h3>
        <div class="score-bar" style="margin-bottom:10px;">
          <span style="font-size:0.82rem;color:#64748b;width:32px;">{score_pct}%</span>
          <div class="score-track">
            <div class="score-fill" style="width:{score_pct}%;background:{score_color};"></div>
          </div>
          <span style="font-size:0.82rem;color:#64748b;">{passed}/{total}</span>
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
                         threshold: float = 0.6, job_id: str = '') -> str:
    """
    Generate a self-contained HTML report.

    Args:
        reports: newly checked reports (full report dicts with gost_results etc.)
        historical: virtual report dicts from memory store (is_historical=True)
        text_plagiarism, image_plagiarism: results from checker modules
        threshold: similarity threshold (0-1)
    """
    now      = datetime.now().strftime('%d.%m.%Y %H:%M')
    n        = len(reports)
    thr_pct  = int(threshold * 100)

    new_paths  = {r['path'] for r in reports}
    hist_paths = {h['path'] for h in historical}

    # Filter historical to only those that have at least one relevant match
    matrix = text_plagiarism.get('matrix', {})
    img_pairs = image_plagiarism.get('pairs', [])

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
        p for pair in img_pairs
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
    for pair in img_pairs:
        if pair['report1'] in hist_paths and pair['report2'] in new_paths:
            cross_session.add(pair['report2'])
        elif pair['report2'] in hist_paths and pair['report1'] in new_paths:
            cross_session.add(pair['report1'])
    cross_session_count = len(cross_session)


    matrix_html = _render_matrix(reports, historical_relevant, text_plagiarism, threshold)


    img_summary = _render_image_summary(image_plagiarism, report_by_path)


    cards = ''.join(
        _render_card(r, text_plagiarism, image_plagiarism, threshold, report_by_path)
        for r in reports
    )


    def _row_class(r):
        path = r['path']
        sim, _ = _max_sim(path, matrix)
        has_plag = sim >= threshold or any(
            p['report1'] == path or p['report2'] == path for p in img_pairs
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
                f'<span style="color:#d97706;font-size:0.75rem;">(база v{v}, {d})</span>'
            )
        elif other_rep:
            anc = _anchor(other_rep)
            other_name_html = f'<a href="#{anc}">{_esc(_display_name(other_rep))}</a>'
        else:
            other_name_html = ','

        p, t = _gost_score(r.get('gost_results', []))
        img_count = sum(
            1 for pair in img_pairs
            if pair['report1'] == path or pair['report2'] == path
        )
        plag_badge = (f'<span class="badge badge-red">{sim:.0%}</span>'
                      if sim >= threshold else
                      f'<span style="color:#64748b;">{sim:.0%}</span>')
        gost_badge = (
            f'<span class="badge badge-green">{p}/{t}</span>' if p == t else
            f'<span class="badge {"badge-amber" if p >= t * 0.7 else "badge-red"}">{p}/{t}</span>'
        )
        img_badge = (f'<span class="badge badge-red">{img_count} дублей</span>'
                     if img_count else '<span style="color:#16a34a;">,</span>')
        anchor = _anchor(r)
        summary_rows.append(
            f'<tr class="{_row_class(r)}">'
            f'<td><a href="#{anchor}">{_esc(_display_name(r))}</a></td>'
            f'<td>{plag_badge} → {other_name_html}</td>'
            f'<td>{img_badge}</td>'
            f'<td>{gost_badge}</td>'
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


    page_rule = f'''@page {{
  size: A4 portrait;
  margin: 15mm 20mm 24mm 20mm;
  @bottom-left {{
    content: "#au-team · АЛЁНА · Проверка студенческих отчётов · ГОСТ 7.32-2017";
    font-size: 7.5pt;
    color: #94a3b8;
    font-family: Arial, sans-serif;
  }}
  @bottom-right {{
    content: "Сформировано: {now}  ·  стр. " counter(page) " / " counter(pages);
    font-size: 7.5pt;
    color: #94a3b8;
    font-family: Arial, sans-serif;
  }}
}}'''

    css = page_rule + '''
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', Arial, sans-serif; background: #f1f5f9; color: #1e293b; line-height: 1.5; font-size: 14px; }
.container { max-width: 1400px; margin: 0 auto; padding: 24px; }
h1 { font-size: 1.7rem; font-weight: 700; }
h2 { font-size: 1.15rem; font-weight: 600; margin: 0 0 12px; }
h3 { font-size: 0.95rem; font-weight: 600; margin-bottom: 8px; }
.subtitle { color: #64748b; font-size: 0.88rem; margin: 6px 0 24px; }
a { color: #3b82f6; text-decoration: none; }
a:hover { text-decoration: underline; }

.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 28px; }
.stat-card { background: white; border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.stat-num { font-size: 2.2rem; font-weight: 700; line-height: 1; }
.stat-lbl { color: #64748b; font-size: 0.82rem; margin-top: 4px; }
.c-blue .stat-num   { color: #3b82f6; }
.c-red .stat-num    { color: #ef4444; }
.c-amber .stat-num  { color: #f59e0b; }
.c-green .stat-num  { color: #22c55e; }

.section { background: white; border-radius: 12px; padding: 22px 24px; box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 22px; }

.summary-table { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
.summary-table th { text-align: left; padding: 8px 12px; background: #f8fafc; color: #64748b; font-weight: 600; border-bottom: 2px solid #e2e8f0; }
.summary-table td { padding: 8px 12px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }
.tr-red   td:first-child { border-left: 3px solid #ef4444; }
.tr-amber td:first-child { border-left: 3px solid #f59e0b; }
.tr-green td:first-child { border-left: 3px solid #22c55e; }

.badge { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 0.76rem; font-weight: 600; }
.badge-red   { background: #fee2e2; color: #dc2626; }
.badge-amber { background: #fef3c7; color: #d97706; }
.badge-green { background: #dcfce7; color: #16a34a; }
.badge-blue  { background: #dbeafe; color: #1d4ed8; }

.matrix-scroll { overflow-x: auto; }
.matrix-table { border-collapse: collapse; font-size: 0.72rem; }
.matrix-table th, .matrix-table td { padding: 5px 7px; border: 1px solid #e2e8f0; text-align: center; white-space: nowrap; }
.matrix-table thead th { background: #f8fafc; font-weight: 600; max-width: 120px; overflow: hidden; text-overflow: ellipsis; writing-mode: vertical-rl; transform: rotate(180deg); height: 90px; }
.matrix-table tbody th { text-align: left; font-weight: 500; background: #f8fafc; max-width: 150px; overflow: hidden; text-overflow: ellipsis; }
.cell-self { background: #e2e8f0 !important; }
.matrix-cell { transition: background 0.15s; }

.report-card { background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 14px; overflow: hidden; }
.report-header { padding: 14px 18px; display: flex; align-items: center; gap: 10px; cursor: pointer; }
.report-header:hover { background: #f8fafc; }
.report-body { padding: 20px; display: none; border-top: 1px solid #f1f5f9; }
.report-body.open { display: block; }
.toggle-arrow { color: #94a3b8; font-size: 0.8rem; transition: transform .2s; margin-left: auto; }
.toggle-arrow.open { transform: rotate(180deg); }

.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
@media (max-width: 860px) { .grid-2 { grid-template-columns: 1fr; } }

.checks-table { width: 100%; border-collapse: collapse; font-size: 0.83rem; }
.checks-table th { text-align: left; padding: 6px 10px; background: #f8fafc; color: #64748b; font-weight: 600; border-bottom: 1px solid #e2e8f0; }
.checks-table td { padding: 6px 10px; border-bottom: 1px solid #f8fafc; vertical-align: top; }
.check-pass { color: #16a34a; font-weight: 700; }
.check-fail { color: #dc2626; font-weight: 700; }
.check-warn { color: #d97706; font-weight: 700; }

.score-bar { display: flex; align-items: center; gap: 8px; }
.score-track { flex: 1; height: 5px; background: #e2e8f0; border-radius: 3px; overflow: hidden; }
.score-fill { height: 100%; border-radius: 3px; }

.plagiarism-alert { background: #fff1f2; border: 1px solid #fecdd3; border-radius: 7px; padding: 10px 14px; margin-bottom: 8px; font-size: 0.86rem; }
.passage { background: #fef9c3; border-left: 3px solid #fbbf24; padding: 8px 12px; margin: 6px 0; border-radius: 0 5px 5px 0; font-size: 0.8rem; font-family: 'Consolas', monospace; white-space: pre-wrap; word-break: break-word; }

.img-pair { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin: 10px 0; padding: 12px; background: #fff7ed; border-radius: 8px; border: 1px solid #fed7aa; }
.img-pair img { max-width: 200px; max-height: 150px; object-fit: contain; border: 1px solid #e2e8f0; border-radius: 4px; background: white; }
.img-info { font-size: 0.8rem; color: #64748b; margin-top: 4px; }

.print-btn { display:inline-flex; align-items:center; gap:6px; padding:8px 18px; background:#015D1E; color:white; border:none; border-radius:7px; font-size:0.84rem; font-weight:700; cursor:pointer; text-decoration:none; }
.print-btn:hover { background:#014818; }
.report-toolbar { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; margin-bottom:20px; }

@media print {
  body { background: white; font-size: 11px; }
  .print-btn, .toggle-arrow { display: none !important; }
  .report-body { display: block !important; }
  .report-header { cursor: default; }
  .report-card, .section { break-inside: avoid; box-shadow: none; border: 1px solid #e2e8f0; }
  .matrix-scroll { overflow: visible; }
  .grid-2 { grid-template-columns: 1fr 1fr; }
  .passage { white-space: pre-wrap; }
  a { color: inherit; text-decoration: none; }
  .container { padding: 12px; }
}
'''

    js = '''
document.querySelectorAll('.report-header').forEach(function(h) {
  h.addEventListener('click', function() {
    var body = this.nextElementSibling;
    var arrow = this.querySelector('.toggle-arrow');
    body.classList.toggle('open');
    if (arrow) arrow.classList.toggle('open');
  });
});
'''

    return f'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>АЛЁНА — проверка отчётов, {now}</title>
<style>{css}</style>
</head>
<body>
<div class="container">

<div class="report-toolbar">
  <div>
    <h1>АЛЁНА <span style="font-weight:600;font-size:.85rem;opacity:.7;">· Проверка студенческих отчётов</span></h1>
    <p class="subtitle">
      Сгенерировано: {now} &nbsp;|&nbsp;
      Порог: {thr_pct}% &nbsp;|&nbsp;
      ГОСТ 7.32-2017
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
  <p style="color:#64748b;font-size:0.83rem;margin-bottom:14px;">
    Жаккар по 5-граммам слов. Красный, выше порога {thr_pct}%, жёлтый, 55-{thr_pct}%.
    {'Включены совпадения с предыдущими сессиями (выделены желтоватым фоном).' if historical_relevant else ''}
  </p>
  {matrix_html}
</div>

{img_summary}

<h2 style="margin-bottom:12px;">Детальный анализ по каждому отчёту</h2>
<p style="color:#64748b;font-size:0.83rem;margin-bottom:14px;">
  Нажмите на карточку, чтобы раскрыть подробности.
</p>
{cards}

</div>
<script>{js}</script>
</body>
</html>'''
