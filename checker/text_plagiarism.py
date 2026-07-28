"""Text plagiarism detection via word n-gram (shingle) Jaccard similarity."""
import re
from collections import defaultdict

SHINGLE_SIZE = 5        # word n-gram size
MIN_PASSAGE_WORDS = 8   # min words to display a common passage


def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[^\w\s]', ' ', text, flags=re.UNICODE)
    text = re.sub(r'\b\d{1,3}\b', ' ', text)   # strip page numbers etc.
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _shingles(text: str, n: int = SHINGLE_SIZE) -> set:
    """Множество хешей словесных n-грамм.

    Хеши, а не сами кортежи слов: для Жаккара нужны только совпадения, а
    отчёт на сорок страниц даёт двенадцать тысяч n-грамм — кортежами это
    2 МБ на работу, и при сравнении с базой в сотни отчётов память кончается.
    Совпадение хешей 64-битное: ложное пересечение на таких объёмах невероятно.
    """
    words = text.split()
    if len(words) < n:
        return {hash(tuple(words))} if words else set()
    return {hash(tuple(words[i:i + n])) for i in range(len(words) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


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
        best_j = -1
        for j in positions[:20]:
            length = n
            while (i + length < len(words1)
                   and j + length < len(words2)
                   and words1[i + length] == words2[j + length]):
                length += 1
            if length > best_len:
                best_len, best_j = length, j

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
        threshold
    """
    norm_texts: dict    = {}
    shingles_map: dict  = {}
    is_historical: dict = {}
    student_key: dict   = {}

    for r in reports:
        path = r['path']
        norm = r.get('normalized_text') or normalize_text(r.get('full_text', ''))
        norm_texts[path]    = norm
        shingles_map[path]  = _shingles(norm)
        is_historical[path] = r.get('is_historical', False)
        s = r.get('student', {})
        sk = f"{s.get('name','').strip().lower()}|{s.get('group','').strip().lower()}"
        student_key[path] = sk if sk != '|' else ''

    paths = list(shingles_map.keys())
    matrix = {p: {p: 1.0} for p in paths}
    flagged = []

    m = len(paths)
    total_pairs = m * (m - 1) // 2
    done_pairs = 0

    for i in range(len(paths)):
        if on_progress is not None:
            on_progress(done_pairs, total_pairs)
            done_pairs += m - 1 - i
        for j in range(i + 1, len(paths)):
            p1, p2 = paths[i], paths[j]

            # Несравнённые пары в матрицу не пишем: она квадратична по числу
            # отчётов, и на базе в тысячу работ миллион нулей — это сотня
            # мегабайт впустую. Читатели матрицы берут отсутствующую ячейку
            # за ноль.

            # Skip: both historical (compared in prior sessions)
            if is_historical[p1] and is_historical[p2]:
                continue

            # Skip: same student (same name + group → comparing with themselves)
            if student_key[p1] and student_key[p1] == student_key[p2]:
                continue

            sim = _jaccard(shingles_map[p1], shingles_map[p2])
            matrix[p1][p2] = sim
            matrix[p2][p1] = sim

            if sim >= threshold:
                w1 = norm_texts[p1].split()
                w2 = norm_texts[p2].split()
                passages = _find_passages(w1, w2)
                flagged.append({
                    'report1':    p1,
                    'report2':    p2,
                    'similarity': sim,
                    'passages':   [p['text'][:300] for p in passages],
                })

    if on_progress is not None:
        on_progress(total_pairs, total_pairs)

    flagged.sort(key=lambda x: -x['similarity'])
    return {'pairs': flagged, 'matrix': matrix, 'threshold': threshold}
