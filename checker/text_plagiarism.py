"""Text plagiarism detection via word n-gram (shingle) Jaccard similarity."""
import hashlib
import itertools
import math
import re
from collections import defaultdict

SHINGLE_SIZE = 5        # word n-gram size
MIN_PASSAGE_WORDS = 8   # min words to display a common passage

# Ниже этого числа слов сравнивать нечем. Из скана или из PDF со сломанной
# кодировкой шрифта извлекается пара слов на весь отчёт – раньше такие работы
# сводились к одному хешу и любые две из них совпадали на 100 %, то есть группа
# сканов обвинялась в списывании целиком. Теперь они выводятся из сравнения и
# помечаются отдельно: заимствование в них проверяют глазами.
MIN_COMPARE_WORDS = 30
PREFILTER_SAMPLE_SIZE = 128


def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[^\w\s]', ' ', text, flags=re.UNICODE)
    text = re.sub(r'\b\d{1,3}\b', ' ', text)   # strip page numbers etc.
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def text_fingerprint(normalized_text: str) -> str:
    """Stable digest used to recognize the same anonymous stored work."""
    if not normalized_text:
        return ''
    return hashlib.sha256(normalized_text.encode('utf-8')).hexdigest()


def report_text_fingerprint(report: dict) -> str:
    stored = report.get('text_hash', '')
    if stored:
        return str(stored)
    normalized = report.get('normalized_text')
    if normalized is None:
        normalized = normalize_text(report.get('full_text', ''))
    return text_fingerprint(normalized)


def _image_fingerprint(report: dict) -> str:
    images = (report.get('precomputed_images', [])
              if report.get('is_historical') else report.get('images', []))
    parts = []
    for image in images:
        hashes = image.get('hashes') or []
        if hashes:
            parts.append(f"{image.get('page', 0)}:"
                         + ','.join(str(value) for value in hashes))
    return text_fingerprint('|'.join(parts))


def anonymous_work_key(report: dict):
    """Filename + exact content digest for a work without a student name."""
    student = report.get('student') or {}
    if str(student.get('name') or '').strip():
        return None
    filename = str(report.get('filename') or '').strip().casefold()
    fingerprint = report_text_fingerprint(report)
    kind = 'text'
    if not fingerprint:
        fingerprint = _image_fingerprint(report)
        kind = 'images'
    return ((filename, kind, fingerprint)
            if filename and fingerprint else None)


def _shingles(text: str, n: int = SHINGLE_SIZE) -> set:
    """Множество хешей словесных n-грамм.

    Хеши, а не сами кортежи слов: для Жаккара нужны только совпадения, а
    отчёт на сорок страниц даёт двенадцать тысяч n-грамм – кортежами это
    2 МБ на работу, и при сравнении с базой в сотни отчётов память кончается.
    Совпадение хешей 64-битное: ложное пересечение на таких объёмах невероятно.

    Текст короче n слов даёт пустое множество, а не один хеш на весь документ:
    иначе две работы с одинаковым огрызком текста совпадали бы на 100 %.
    """
    words = text.split()
    if len(words) < n:
        return set()
    return {hash(tuple(words[i:i + n])) for i in range(len(words) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a) + len(b) - inter
    return inter / union if union else 0.0


def _shingle_sample(shingles: set) -> set:
    """Up to 128 hashes for the cheap first pass over a candidate."""
    if len(shingles) <= PREFILTER_SAMPLE_SIZE:
        return shingles
    return set(itertools.islice(shingles, PREFILTER_SAMPLE_SIZE))


def _candidate_jaccard(a: set, sample_a: set, b: set, sample_b: set,
                       threshold: float):
    """Exact Jaccard for a viable candidate, otherwise ``None`` early.

    The 128-value sample is checked first. We then inspect only as much of the
    smaller set as needed to prove that the required intersection is
    unreachable. Unlike rejecting on a missing sample overlap, this has no
    false negatives at ``threshold``.
    """
    if not a or not b:
        return None
    if len(a) <= len(b):
        source, sample, other = a, sample_a, b
    else:
        source, sample, other = b, sample_b, a

    def _can_reach(intersection: int, remaining: int) -> bool:
        upper_intersection = min(len(other), intersection + remaining)
        upper_union = len(a) + len(b) - upper_intersection
        upper_similarity = (upper_intersection / upper_union
                            if upper_union else 0.0)
        return upper_similarity >= threshold

    if not _can_reach(0, len(source)):
        return None

    checked = intersection = 0
    for value in sample:
        checked += 1
        if value in other:
            intersection += 1
    if not _can_reach(intersection, len(source) - checked):
        return None

    for value in source:
        if value in sample:
            continue
        checked += 1
        if value in other:
            intersection += 1
        if not _can_reach(intersection, len(source) - checked):
            return None

    union = len(a) + len(b) - intersection
    return intersection / union if union else 0.0


def _find_passages(words1: list, words2: list) -> list:
    """Find verbatim shared passages (word sequences >= MIN_PASSAGE_WORDS)."""
    n = MIN_PASSAGE_WORDS
    if len(words1) < n or len(words2) < n:
        return []

    idx2: dict = defaultdict(list)
    for i in range(len(words2) - n + 1):
        gram = tuple(words2[i:i + n])
        idx2[gram].append(i)

    passages = []
    covered: set = set()

    for i in range(len(words1) - n + 1):
        if i in covered:
            continue
        gram = tuple(words1[i:i + n])
        positions = idx2.get(gram, [])
        if not positions:
            continue

        best_len = 0
        for j in positions[:20]:
            length = n
            while (i + length < len(words1)
                   and j + length < len(words2)
                   and words1[i + length] == words2[j + length]):
                length += 1
            best_len = max(best_len, length)

        if best_len >= n:
            passages.append({
                'text':  ' '.join(words1[i:i + best_len]),
                'words': best_len,
                'pos1':  i,
            })
            for k in range(i, i + best_len):
                covered.add(k)

    passages.sort(key=lambda x: -x['words'])
    return passages[:6]


def check_text_plagiarism(reports: list, threshold: float = 0.6,
                          on_progress=None) -> dict:
    """
    Pairwise shingle-based similarity for all reports (including historical).

    Historical-vs-historical pairs are skipped (already compared in prior sessions).

    on_progress(сравнено_пар, всего_пар) вызывается по ходу сравнения: на
    большой партии этот этап идёт минутами, и без него полоса выполнения
    замирает.

    Returns:
        pairs: flagged pairs sorted by similarity desc
        matrix: {path: {path: float}}
        no_text: пути работ, из которых текста извлеклось слишком мало для
                 сравнения – они не участвуют ни в одной паре
        threshold
    """
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 0.6
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        threshold = 0.6

    records = []
    no_text: set = set()
    for report in reports:
        path = report['path']
        norm = (report.get('normalized_text')
                or normalize_text(report.get('full_text', '')))
        if len(norm.split()) < MIN_COMPARE_WORDS:
            no_text.add(path)
        student = report.get('student', {})
        name = str(student.get('name') or '').strip().lower()
        group = str(student.get('group') or '').strip().lower()
        records.append({
            'path': path,
            'norm': norm,
            'historical': bool(report.get('is_historical', False)),
            'student': f'{name}|{group}' if name else '',
            'anonymous': anonymous_work_key(report),
        })

    current = [record for record in records if not record['historical']]
    historical = [record for record in records if record['historical']]
    current_sets = {
        record['path']: (_shingles(record['norm'])
                         if record['path'] not in no_text else set())
        for record in current
    }
    current_samples = {path: _shingle_sample(shingles)
                       for path, shingles in current_sets.items()}

    paths = [record['path'] for record in records]
    matrix = {p: {p: 1.0} for p in paths}
    flagged = []
    compared = defaultdict(int)
    total_pairs = (len(current) * (len(current) - 1) // 2
                   + len(current) * len(historical))
    done_pairs = 0

    def _compare(first, first_set, first_sample,
                 second, second_set, second_sample, prefilter):
        p1, p2 = first['path'], second['path']
        if p1 in no_text or p2 in no_text:
            return
        if (first['historical'] != second['historical']
                and first['anonymous']
                and first['anonymous'] == second['anonymous']):
            return
        if first['student'] and first['student'] == second['student']:
            return

        compared[p1] += 1
        compared[p2] += 1

        sim = (_candidate_jaccard(
            first_set, first_sample, second_set, second_sample, threshold)
            if prefilter and threshold > 0
            else _jaccard(first_set, second_set))
        if sim is None:
            return
        matrix[p1][p2] = sim
        matrix[p2][p1] = sim
        if sim < threshold:
            return

        passages = _find_passages(first['norm'].split(), second['norm'].split())
        flagged.append({
            'report1': p1,
            'report2': p2,
            'similarity': sim,
            'passages': [passage['text'][:300] for passage in passages],
        })

    # The current batch is bounded by the upload and must be compared exactly.
    for i, first in enumerate(current):
        if on_progress is not None:
            on_progress(done_pairs, total_pairs)
        for second in current[i + 1:]:
            _compare(
                first, current_sets[first['path']],
                current_samples[first['path']],
                second, current_sets[second['path']],
                current_samples[second['path']], False,
            )
        done_pairs += len(current) - 1 - i

    # Expand one historical report at a time and release its full shingle set
    # before moving to the next record. A thousand-report base no longer means
    # a thousand Python sets resident at once.
    for old in historical:
        if on_progress is not None:
            on_progress(done_pairs, total_pairs)
        old_set = (_shingles(old['norm'])
                   if old['path'] not in no_text else set())
        old_sample = _shingle_sample(old_set)
        for fresh in current:
            _compare(
                fresh, current_sets[fresh['path']],
                current_samples[fresh['path']],
                old, old_set, old_sample, True,
            )
        done_pairs += len(current)

    if on_progress is not None:
        on_progress(total_pairs, total_pairs)

    flagged.sort(key=lambda x: -x['similarity'])
    return {'pairs': flagged, 'matrix': matrix, 'threshold': threshold,
            'no_text': sorted(no_text), 'compared': dict(compared)}
