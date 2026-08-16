"""Compact result summary stored alongside the job history.

The full HTML report stays the authoritative output; this is the small digest
the UI needs to show a check without opening it: GOST score, worst borrowing,
per-student rows and the matches that were flagged.
"""

from collections import Counter

from . import grading

MAX_MATCHES = 500   # сколько совпадений хранится в дайджесте, см. build()


def _gost_pct(report: dict) -> int:
    results = report.get('gost_results', []) or []
    if not results:
        return 0
    passed = sum(1 for c in results if c.get('passed'))
    return round(passed / len(results) * 100)


def _failed_codes(report: dict) -> list:
    return [c['code'] for c in (report.get('gost_results', []) or [])
            if not c.get('passed')]


def _display_name(report: dict) -> str:
    student = report.get('student', {}) or {}
    # Когда фамилию из отчёта вытащить не удалось, показываем имя файла – но
    # только начало: выгрузка Moodle даёт имена в две сотни символов, и таблица
    # от них разъезжается. В отчёте подрезано так же (reporter._display_name).
    return (student.get('name')
            or (report.get('filename') or '')[:60]
            or 'Без имени')


def _group_of(report: dict) -> str:
    return (report.get('student', {}) or {}).get('group', '') or ''


def _where(report: dict) -> str:
    if report.get('is_historical'):
        date = report.get('historical_date', '')
        return f'база, {date}' if date else 'база отчётов'
    return 'в этой пачке'


def build(reports: list, historical: list, text_plag: dict, img_plag: dict,
          threshold: float, weights: dict = None,
          scale: int = grading.DEFAULT_SCALE) -> dict:
    """Digest of one finished check. `reports` are the freshly uploaded ones.

    `weights` and `scale` are captured per job: the recommended grade must stay
    what it was when the check ran, even if the administrator later reweighs
    the criteria.
    """
    by_path = {r.get('path'): r for r in (reports + historical)}
    matrix = text_plag.get('matrix', {}) or {}
    no_text = set(text_plag.get('no_text') or ())
    weights = weights or {}

    students = []
    for r in reports:
        if r.get('error'):
            students.append({
                'fio':     _display_name(r),
                'group':   _group_of(r),
                'gost':    None,
                'plag':    None,
                'no_text': False,
                'fails':   [],
                'flaws':   [],
                'grade':   None,
                'error':   str(r.get('error')),
            })
            continue
        # The matrix keeps a 1.0 diagonal (a report matches itself), so the
        # report's own path has to be dropped before taking the worst match.
        path = r.get('path')
        row = {p: v for p, v in (matrix.get(path, {}) or {}).items() if p != path}
        worst = max(row.values()) if row else 0.0
        results = r.get('gost_results', []) or []
        # Работу без извлекаемого текста не с чем сравнивать: доля заимствования
        # у неё не «0 %», а «неизвестна», иначе скан выглядел бы чистым.
        unreadable = path in no_text
        students.append({
            'fio':     _display_name(r),
            'group':   _group_of(r),
            'gost':    _gost_pct(r),
            'plag':    None if unreadable else round(worst * 100),
            'no_text': unreadable,
            'fails':   _failed_codes(r),
            'flaws':   grading.flaws(results),
            'grade':   grading.grade(results, weights, scale),
            'error':   None,
        })

    matches = []
    for pair in text_plag.get('pairs', []) or []:
        a = by_path.get(pair.get('report1'))
        b = by_path.get(pair.get('report2'))
        if a is None or b is None:
            continue
        # Report the pair from the point of view of the newly uploaded work.
        if a.get('is_historical') and not b.get('is_historical'):
            a, b = b, a
        matches.append({
            'a':     _display_name(a),
            'b':     _display_name(b) + (f' · {_group_of(b)}' if _group_of(b) else ''),
            'pct':   round(pair.get('similarity', 0) * 100),
            'kind':  'текст',
            'where': _where(b),
        })

    # Одна и та же пара работ может делить сразу несколько страниц-картинок –
    # это раньше давало по строке на страницу (десяток строк на пару работ
    # с общим шаблоном). Схлопываем их в одну строку со списком страниц.
    img_groups = {}
    for pair in img_plag.get('pairs', []) or []:
        if pair.get('ui_review'):
            continue          # screenshots of the same UI are not borrowing
        a = by_path.get(pair.get('report1'))
        b = by_path.get(pair.get('report2'))
        if a is None or b is None:
            continue
        if a.get('is_historical') and not b.get('is_historical'):
            a, b = b, a
            page = pair.get('page2')
        else:
            page = pair.get('page1')
        key = (a.get('path'), b.get('path'))
        group = img_groups.setdefault(key, {'a': a, 'b': b, 'pages': []})
        if page:
            group['pages'].append(page)

    for group in img_groups.values():
        a, b = group['a'], group['b']
        pages = sorted(set(group['pages']))
        if not pages:
            kind = 'изображение'
        elif len(pages) == 1:
            kind = f'изображение, стр. {pages[0]}'
        else:
            kind = 'изображения, стр. ' + ', '.join(str(p) for p in pages)
        matches.append({
            'a':     _display_name(a),
            'b':     _display_name(b) + (f' · {_group_of(b)}' if _group_of(b) else ''),
            'pct':   None,     # image duplicates are a yes/no match, not a share
            'kind':  kind,
            'where': _where(b),
        })

    matches.sort(key=lambda m: (m['pct'] is None, -(m['pct'] or 0)))
    # Полсотни работ с одинаковыми скриншотами дают десятки тысяч совпадений.
    # Дайджест лежит в истории и уходит в браузер при каждом обновлении списка
    # проверок, поэтому храним самые заметные, а общее число – отдельным полем.
    matches_total = len(matches)
    matches = matches[:MAX_MATCHES]

    scored = [s for s in students if s['gost'] is not None]
    groups = sorted({s['group'] for s in students if s['group']})
    fails = Counter(code for s in students for code in s['fails'])
    # Максимум берётся только по работам, которые вообще удалось сравнить:
    # у остальных заимствование не ноль, а неизвестно.
    plags = [s['plag'] for s in scored if s['plag'] is not None]

    grades = [s['grade']['pct'] for s in scored
              if s['grade'] and s['grade']['pct'] is not None]
    grade_pct = round(sum(grades) / len(grades)) if grades else None

    return {
        'group':       groups[0] if groups else '–',
        'groups':      groups,
        'gost':        round(sum(s['gost'] for s in scored) / len(scored)) if scored else 0,
        'plag':        max(plags, default=0),
        'no_text':     sum(1 for s in students if s.get('no_text')),
        'threshold':   round(threshold * 100),
        'students':    students,
        'matches':     matches,
        'matches_total': matches_total,
        'fail_counts': fails.most_common(),
        'clean':       sum(1 for s in scored if not s['fails']),
        'grade':       grade_pct,
        'grade_score': grading.as_score(grade_pct, scale),
        'scale':       scale,
        # веса, отличные от равных, меняют смысл оценки – это видно в интерфейсе
        'weighted':    any(v != grading.DEFAULT_WEIGHT for v in weights.values()),
    }
