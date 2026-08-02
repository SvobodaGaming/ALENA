"""Рекомендуемая оценка за оформление и готовый отзыв студенту.

У каждого критерия есть «процент использования» — 0…100. Веса критериев,
выбранных для конкретной проверки, приводятся к сумме 100, и оценка равна доле
этой сотни, которую работа набрала пройденными критериями. Критерий с весом 0
по-прежнему проверяется и попадает в отзыв — он просто не двигает оценку.
"""

from .gost import ALL_CODES, CHECK_NAMES, FLAW_TEXT

DEFAULT_WEIGHT = 100    # изначально все критерии равнозначны
DEFAULT_SCALE = 100     # 100 — оценка в процентах; иначе шкала в баллах


def clean_weights(raw) -> dict:
    """Веса из формы, JSON или API → {код: 0…100}. Мусор отбрасывается."""
    if not raw:
        return {}
    if isinstance(raw, str):
        pairs = []
        for chunk in raw.replace(';', ',').split(','):
            if ':' in chunk:
                code, _, value = chunk.partition(':')
                pairs.append((code, value))
        raw = dict(pairs)
    out = {}
    for code, value in (raw or {}).items():
        code = str(code).strip().upper()
        if code not in ALL_CODES:
            continue
        try:
            out[code] = max(0, min(100, int(round(float(value)))))
        except (TypeError, ValueError):
            continue
    return out


def clean_scale(raw) -> int:
    """Шкала оценки: целое 2…100. Значение вне диапазона → проценты."""
    try:
        scale = int(round(float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_SCALE
    return scale if 2 <= scale <= 100 else DEFAULT_SCALE


def shares(weights: dict, codes) -> dict:
    """Нормированные веса: {код: процент}, в сумме 100 по выбранным критериям."""
    codes = list(codes)
    if not codes:
        return {}
    raw = {c: max(0, min(100, int((weights or {}).get(c, DEFAULT_WEIGHT))))
           for c in codes}
    total = sum(raw.values())
    if total == 0:
        # Все веса обнулены — вырожденный случай; считаем критерии равными,
        # иначе оценка была бы неопределённой при любом результате проверки.
        return {c: 100 / len(codes) for c in codes}
    return {c: v * 100 / total for c, v in raw.items()}


def as_score(pct, scale: int = DEFAULT_SCALE):
    """Процент → балл выбранной шкалы. При шкале 100 балл не нужен."""
    if pct is None or scale == DEFAULT_SCALE:
        return None
    return round(pct * scale / 100, 1)


def grade(gost_results: list, weights: dict = None,
          scale: int = DEFAULT_SCALE) -> dict:
    """Рекомендуемая оценка за оформление одной работы.

    pct   — 0…100, доля веса пройденных критериев;
    score — та же оценка в баллах шкалы (None, когда шкала — проценты);
    lost  — что именно стоило баллов, от самого дорогого критерия к дешёвому.
    """
    results = gost_results or []
    codes = [c['code'] for c in results]
    share = shares(weights, codes)

    earned = sum(share.get(c['code'], 0) for c in results if c.get('passed'))
    lost = [
        {'code': c['code'],
         'name': c.get('name') or CHECK_NAMES.get(c['code'], c['code']),
         'weight': round(share.get(c['code'], 0), 1)}
        for c in results if not c.get('passed')
    ]
    lost.sort(key=lambda x: -x['weight'])

    pct = round(earned) if results else None
    return {
        'pct':      pct,
        'score':    as_score(pct, scale),
        'scale':    scale,
        'lost':     lost,
        'criteria': len(results),
    }


def flaws(gost_results: list) -> list:
    """Непройденные критерии как замечания: [{code, text, details}].

    Порядок сохраняется — он совпадает с порядком критериев в ГОСТ-таблице,
    так что отзыв читается сверху вниз: сперва структура, потом оформление.
    """
    out = []
    for c in gost_results or []:
        if c.get('passed'):
            continue
        code = c.get('code', '')
        out.append({
            'code':    code,
            'text':    FLAW_TEXT.get(code) or c.get('name') or code,
            'details': (c.get('details') or '').strip(),
        })
    return out


def feedback_lines(student: dict, threshold_pct: int = None,
                   details: bool = False) -> list:
    """Строки готового отзыва по одной записи `students` из summary."""
    if student.get('error'):
        return [f'Файл не удалось прочитать: {student["error"]}']

    lines = []
    for flaw in student.get('flaws', []):
        line = flaw['text']
        if details and flaw.get('details'):
            line += f' ({flaw["details"]})'
        lines.append(line)

    if student.get('no_text'):
        lines.append('Текст из файла не извлекается — скорее всего это скан или '
                     'нестандартные шрифты. Заимствование автоматически не '
                     'проверено, нужна ручная проверка')

    plag = student.get('plag')
    if plag is not None and threshold_pct is not None and plag >= threshold_pct:
        lines.append(f'Совпадение с другой работой — {plag}% '
                     f'(допустимый порог {threshold_pct}%)')
    return lines


def feedback_text(student: dict, threshold_pct: int = None,
                  details: bool = False, bullet: str = '• ') -> str:
    """Готовый к вставке отзыв: заголовок, перечень замечаний, оценка."""
    lines = feedback_lines(student, threshold_pct, details)
    head = student.get('fio') or 'Работа'
    if student.get('group'):
        head += f', {student["group"]}'

    if not lines:
        body = 'Замечаний по оформлению нет.'
    else:
        body = 'Замечания по оформлению:\n' + '\n'.join(bullet + l for l in lines)

    g = student.get('grade') or {}
    tail = ''
    if g.get('pct') is not None:
        tail = f'\n\nРекомендуемая оценка за оформление: {g["pct"]}%'
        if g.get('score') is not None:
            tail += f' ({g["score"]:g} из {g["scale"]})'
    return f'{head}\n\n{body}{tail}'
