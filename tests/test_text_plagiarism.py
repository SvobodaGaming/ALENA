"""Заимствование текста: нормализация, шинглы и правила сравнения.

Отдельные случаи обезличенных работ и их истории живут в
tests/test_regressions.py – там они записаны по следам конкретных ошибок.
"""

import unittest

from checker import text_plagiarism as tp
from checker.text_plagiarism import (
    check_text_plagiarism,
    normalize_text,
    report_text_fingerprint,
    text_fingerprint,
)

WORDS = ['альфа', 'бета', 'гамма', 'дельта', 'эпсилон', 'дзета', 'эта', 'тета',
         'йота', 'каппа', 'лямбда', 'мю', 'ню', 'кси', 'омикрон', 'пи', 'ро',
         'сигма', 'тау', 'ипсилон', 'фи', 'хи', 'пси', 'омега']


def text(count=60, tag='альфа'):
    """Текст длиннее MIN_COMPARE_WORDS. У работ с разными tag нет ни одной
    общей пятёрки слов, у одинаковых – совпадение полное."""
    return ' '.join(f'{tag}{i}' for i in range(count))


def work(path, body=None, **overrides):
    data = {'path': path, 'filename': path.rsplit('/', 1)[-1],
            'student': {'name': '', 'group': ''},
            'full_text': text() if body is None else body, 'images': []}
    data.update(overrides)
    return data


class NormalizeTests(unittest.TestCase):
    def test_case_and_punctuation_do_not_survive(self):
        self.assertEqual(normalize_text('Привет, МИР!'), 'привет мир')

    def test_runs_of_whitespace_collapse(self):
        self.assertEqual(normalize_text('раз\n\n  два\tтри'), 'раз два три')

    def test_short_numbers_are_dropped(self):
        """Номера страниц и пунктов иначе попадали бы в шинглы и роднили бы
        работы, ничего общего не имеющие."""
        self.assertEqual(normalize_text('глава 1 стр 42 текст'), 'глава стр текст')

    def test_long_numbers_stay(self):
        self.assertEqual(normalize_text('в 2025 году'), 'в 2025 году')

    def test_empty_text_normalizes_to_empty(self):
        self.assertEqual(normalize_text(''), '')


class FingerprintTests(unittest.TestCase):
    def test_the_same_text_gives_the_same_digest(self):
        self.assertEqual(text_fingerprint('раз два'), text_fingerprint('раз два'))

    def test_different_texts_give_different_digests(self):
        self.assertNotEqual(text_fingerprint('раз два'),
                            text_fingerprint('раз три'))

    def test_empty_text_has_no_digest(self):
        self.assertEqual(text_fingerprint(''), '')

    def test_a_stored_digest_is_trusted_over_recomputing(self):
        report = {'text_hash': 'сохранённый', 'full_text': 'что-то другое'}
        self.assertEqual(report_text_fingerprint(report), 'сохранённый')

    def test_without_a_stored_digest_it_is_computed_from_the_text(self):
        report = {'full_text': 'Привет, МИР!'}
        self.assertEqual(report_text_fingerprint(report),
                         text_fingerprint('привет мир'))


class ShingleTests(unittest.TestCase):
    def test_a_fragment_shorter_than_the_window_yields_nothing(self):
        """Иначе две работы с одинаковым огрызком текста совпадали бы на 100 %."""
        self.assertEqual(tp._shingles('раз два три'), set())

    def test_the_window_slides_by_one_word(self):
        self.assertEqual(len(tp._shingles(' '.join(WORDS[:10]))),
                         10 - tp.SHINGLE_SIZE + 1)

    def test_jaccard_of_a_text_with_itself_is_one(self):
        shingles = tp._shingles(text())
        self.assertEqual(tp._jaccard(shingles, shingles), 1.0)

    def test_jaccard_with_nothing_in_common_is_zero(self):
        self.assertEqual(tp._jaccard(tp._shingles(text()), set()), 0.0)


class ComparisonTests(unittest.TestCase):
    def test_two_copies_of_one_work_match_completely(self):
        result = check_text_plagiarism([work('/a.pdf'), work('/b.pdf')],
                                       threshold=0.6)
        self.assertEqual(len(result['pairs']), 1)
        self.assertEqual(result['pairs'][0]['similarity'], 1.0)

    def test_unrelated_works_are_not_flagged(self):
        result = check_text_plagiarism(
            [work('/a.pdf'), work('/b.pdf', body=text(tag='бета'))], threshold=0.6)
        self.assertEqual(result['pairs'], [])

    def test_a_work_always_matches_itself_in_the_matrix(self):
        result = check_text_plagiarism([work('/a.pdf')], threshold=0.6)
        self.assertEqual(result['matrix']['/a.pdf']['/a.pdf'], 1.0)

    def test_the_threshold_decides_what_counts_as_borrowing(self):
        # Половина текста общая: примерно треть по Жаккару.
        half = text(30)
        pair = [work('/a.pdf', body=half + ' ' + text(30, tag='бета')),
                work('/b.pdf', body=half + ' ' + text(30, tag='гамма'))]
        self.assertEqual(check_text_plagiarism(pair, threshold=0.6)['pairs'], [])
        self.assertEqual(len(check_text_plagiarism(pair, threshold=0.2)['pairs']), 1)

    def test_the_similarity_still_lands_in_the_matrix_below_the_threshold(self):
        """Порог решает, что показать списком; матрица нужна целиком – по ней
        считается «худшее совпадение» каждой работы."""
        half = text(30)
        pair = [work('/a.pdf', body=half + ' ' + text(30, tag='бета')),
                work('/b.pdf', body=half + ' ' + text(30, tag='гамма'))]
        matrix = check_text_plagiarism(pair, threshold=0.6)['matrix']
        self.assertGreater(matrix['/a.pdf']['/b.pdf'], 0)

    def test_a_junk_threshold_falls_back_to_the_default(self):
        for value in (None, 'много', float('nan'), -1, 2):
            result = check_text_plagiarism([work('/a.pdf')], threshold=value)
            self.assertEqual(result['threshold'], 0.6)

    def test_a_work_with_too_little_text_is_set_aside(self):
        """Из скана извлекается пара слов на весь отчёт; раньше такие работы
        сводились к одному хешу и группа сканов обвинялась в списывании
        целиком."""
        short = work('/scan.pdf', body='две строки текста')
        result = check_text_plagiarism([short, work('/a.pdf')], threshold=0.6)
        self.assertEqual(result['no_text'], ['/scan.pdf'])
        self.assertEqual(result['pairs'], [])

    def test_two_scans_are_not_compared_with_each_other_either(self):
        result = check_text_plagiarism(
            [work('/s1.pdf', body='коротко'), work('/s2.pdf', body='коротко')],
            threshold=0.6)
        self.assertEqual(result['pairs'], [])
        self.assertEqual(len(result['no_text']), 2)

    def test_the_same_student_resubmitting_is_not_plagiarism(self):
        student = {'name': 'Иванов Иван', 'group': 'ИВТ-201'}
        fresh = work('/new.pdf', student=student)
        old = work('memory://k|v1', student=student, is_historical=True,
                   normalized_text=normalize_text(text()))
        result = check_text_plagiarism([fresh, old], threshold=0.6)
        self.assertEqual(result['pairs'], [])

    def test_a_different_student_with_the_same_text_is_flagged(self):
        fresh = work('/new.pdf', student={'name': 'Иванов Иван', 'group': 'ИВТ'})
        old = work('memory://k|v1', student={'name': 'Петров Пётр', 'group': 'ИВТ'},
                   is_historical=True, normalized_text=normalize_text(text()))
        result = check_text_plagiarism([fresh, old], threshold=0.6)
        self.assertEqual(len(result['pairs']), 1)

    def test_two_stored_works_are_not_compared_again(self):
        """Пара из базы уже сравнивалась в своей сессии."""
        old = [work(f'memory://k{i}|v1', is_historical=True,
                    normalized_text=normalize_text(text())) for i in (1, 2)]
        self.assertEqual(check_text_plagiarism(old, threshold=0.6)['pairs'], [])

    def test_progress_is_reported_and_ends_at_the_total(self):
        seen = []
        check_text_plagiarism([work(f'/{i}.pdf') for i in range(4)],
                              threshold=0.6, on_progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(seen)
        self.assertEqual(seen[-1], (6, 6))   # 4 работы – 6 пар

    def test_an_empty_batch_is_handled(self):
        result = check_text_plagiarism([], threshold=0.6)
        self.assertEqual(result['pairs'], [])
        self.assertEqual(result['matrix'], {})


class PassageTests(unittest.TestCase):
    def test_a_verbatim_run_is_quoted_back(self):
        shared = ' '.join(WORDS[:12])
        first = work('/a.pdf', body=shared + ' ' + text(50, tag='омега'))
        second = work('/b.pdf', body=shared + ' ' + text(50, tag='омега'))
        pair = check_text_plagiarism([first, second], threshold=0.5)['pairs'][0]
        self.assertTrue(any(shared in passage for passage in pair['passages']))

    def test_a_run_shorter_than_the_minimum_is_not_quoted(self):
        words1 = WORDS[:tp.MIN_PASSAGE_WORDS - 1]
        self.assertEqual(tp._find_passages(words1, list(words1)), [])

    def test_passages_are_listed_longest_first(self):
        long_run = WORDS[:14]
        short_run = WORDS[14:24]
        words = long_run + ['разрыв'] + short_run
        passages = tp._find_passages(words, words)
        self.assertEqual([p['words'] for p in passages],
                         sorted((p['words'] for p in passages), reverse=True))


if __name__ == '__main__':
    unittest.main()
