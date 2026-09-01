"""Дайджест проверки: то, что интерфейс показывает, не открывая отчёт."""

import unittest

from checker import grading, summary


def work(path, name='Иванов Иван', group='ИВТ-201', gost=None, **overrides):
    """Проверенная работа в том виде, в каком её видит дайджест."""
    data = {
        'path':     path,
        'filename': path.rsplit('/', 1)[-1],
        'student':  {'name': name, 'group': group},
        'gost_results': [] if gost is None else gost,
        'error':    None,
    }
    data.update(overrides)
    return data


def criteria(passed=(), failed=()):
    return ([{'code': c, 'name': c, 'passed': True, 'flaw': '', 'details': ''}
             for c in passed]
            + [{'code': c, 'name': c, 'passed': False, 'flaw': '', 'details': ''}
               for c in failed])


def build(reports, historical=(), matrix=None, pairs=(), no_text=(),
          img_pairs=(), threshold=0.6, **kwargs):
    return summary.build(
        list(reports), list(historical),
        {'matrix': matrix or {}, 'pairs': list(pairs), 'no_text': list(no_text)},
        {'pairs': list(img_pairs)},
        threshold, **kwargs)


class StudentRowTests(unittest.TestCase):
    def test_gost_percentage_is_the_share_of_passed_criteria(self):
        report = work('/a.pdf', gost=criteria(passed=['S1', 'S2', 'S3'],
                                              failed=['F1']))
        row = build([report])['students'][0]
        self.assertEqual(row['gost'], 75)
        self.assertEqual(row['fails'], ['F1'])

    def test_a_work_matches_itself_and_that_is_not_borrowing(self):
        """В матрице есть диагональ 1.0; без её отбрасывания у каждой работы
        было бы стопроцентное заимствование."""
        matrix = {'/a.pdf': {'/a.pdf': 1.0, '/b.pdf': 0.25}}
        row = build([work('/a.pdf'), work('/b.pdf')], matrix=matrix)['students'][0]
        self.assertEqual(row['plag'], 25)

    def test_a_work_with_no_comparable_text_has_unknown_borrowing(self):
        """У скана заимствование не «0 %», а неизвестно – иначе он выглядел бы
        чистым, хотя его никто не сравнивал."""
        row = build([work('/scan.pdf')], no_text=['/scan.pdf'])['students'][0]
        self.assertIsNone(row['plag'])
        self.assertTrue(row['no_text'])
        self.assertIsNone(row['grade'])

    def test_an_unreadable_file_keeps_its_row_with_the_error(self):
        row = build([work('/bad.pdf', error='PDF не открылся')])['students'][0]
        self.assertEqual(row['error'], 'PDF не открылся')
        self.assertIsNone(row['gost'])
        self.assertIsNone(row['grade'])

    def test_a_work_without_a_recognised_name_falls_back_to_the_filename(self):
        row = build([work('/x.pdf', name='')])['students'][0]
        self.assertEqual(row['fio'], 'x.pdf')

    def test_a_very_long_filename_is_trimmed_for_the_table(self):
        """Выгрузка Moodle даёт имена в две сотни символов, и таблица от них
        разъезжается."""
        long_name = 'и' * 200 + '.pdf'
        row = build([work('/' + long_name, name='')])['students'][0]
        self.assertEqual(len(row['fio']), 60)


class DigestTests(unittest.TestCase):
    def test_group_is_taken_from_the_batch(self):
        self.assertEqual(build([work('/a.pdf', group='ИВТ-201')])['group'],
                         'ИВТ-201')

    def test_a_batch_without_groups_shows_a_dash(self):
        self.assertEqual(build([work('/a.pdf', group='')])['group'], '–')

    def test_average_gost_counts_only_the_works_that_were_scored(self):
        reports = [work('/a.pdf', gost=criteria(passed=['S1', 'S2'])),
                   work('/b.pdf', gost=criteria(failed=['S1', 'S2'])),
                   work('/c.pdf', error='не открылся')]
        self.assertEqual(build(reports)['gost'], 50)

    def test_worst_borrowing_ignores_works_that_could_not_be_compared(self):
        matrix = {'/a.pdf': {'/b.pdf': 0.4}, '/b.pdf': {'/a.pdf': 0.4}}
        digest = build([work('/a.pdf'), work('/scan.pdf'), work('/b.pdf')],
                       matrix=matrix, no_text=['/scan.pdf'])
        self.assertEqual(digest['plag'], 40)
        self.assertEqual(digest['no_text'], 1)

    def test_clean_counts_works_without_a_single_failed_criterion(self):
        reports = [work('/a.pdf', gost=criteria(passed=['S1'])),
                   work('/b.pdf', gost=criteria(failed=['S1']))]
        self.assertEqual(build(reports)['clean'], 1)

    def test_failed_criteria_are_counted_across_the_batch(self):
        reports = [work('/a.pdf', gost=criteria(failed=['S1', 'F2'])),
                   work('/b.pdf', gost=criteria(failed=['S1']))]
        self.assertEqual(build(reports)['fail_counts'][0], ('S1', 2))

    def test_threshold_is_stored_as_a_percentage(self):
        self.assertEqual(build([work('/a.pdf')], threshold=0.35)['threshold'], 35)

    def test_uneven_weights_are_flagged_for_the_interface(self):
        """Оценка, посчитанная по неравным весам, значит не то же самое, что
        обычная, и интерфейс обязан это показать."""
        reports = [work('/a.pdf', gost=criteria(passed=['S1'], failed=['F2']))]
        self.assertFalse(build(reports)['weighted'])
        self.assertTrue(build(reports, weights={'S1': 100, 'F2': 20})['weighted'])

    def test_the_grade_is_captured_on_the_chosen_scale(self):
        reports = [work('/a.pdf', gost=criteria(passed=['S1'], failed=['F2']))]
        digest = build(reports, scale=5)
        self.assertEqual(digest['grade'], 50)
        self.assertEqual(digest['grade_score'], 2.5)
        self.assertEqual(digest['scale'], 5)

    def test_an_empty_batch_does_not_crash(self):
        digest = build([])
        self.assertEqual(digest['students'], [])
        self.assertEqual(digest['gost'], 0)
        self.assertEqual(digest['group'], '–')


class MatchTests(unittest.TestCase):
    def historical(self, path='memory://teacher|петров|ивт-101|v1',
                   name='Петров Пётр'):
        return work(path, name=name, group='ИВТ-101', is_historical=True,
                    historical_date='01.09.2025')

    def test_a_pair_is_reported_from_the_new_work_point_of_view(self):
        new, old = work('/a.pdf'), self.historical()
        pairs = [{'report1': old['path'], 'report2': new['path'],
                  'similarity': 0.8}]
        match = build([new], [old], pairs=pairs)['matches'][0]
        self.assertEqual(match['a_fio'], 'Иванов Иван')
        self.assertEqual(match['b_fio'], 'Петров Пётр')
        self.assertFalse(match['b_new'])
        self.assertEqual(match['where'], 'база, 01.09.2025')

    def test_a_pair_inside_the_batch_says_so(self):
        first, second = work('/a.pdf'), work('/b.pdf', name='Сидоров Сидор')
        pairs = [{'report1': first['path'], 'report2': second['path'],
                  'similarity': 0.9}]
        match = build([first, second], pairs=pairs)['matches'][0]
        self.assertEqual(match['where'], 'в этой пачке')
        self.assertTrue(match['b_new'])
        self.assertEqual(match['pct'], 90)

    def test_a_pair_naming_an_unknown_work_is_skipped(self):
        pairs = [{'report1': '/a.pdf', 'report2': '/gone.pdf', 'similarity': 0.9}]
        self.assertEqual(build([work('/a.pdf')], pairs=pairs)['matches'], [])

    def test_shared_image_pages_collapse_into_one_row(self):
        """Пара работ с общим шаблоном давала по строке на каждую страницу –
        десяток строк об одном и том же."""
        first, second = work('/a.pdf'), work('/b.pdf', name='Сидоров Сидор')
        img_pairs = [{'report1': first['path'], 'report2': second['path'],
                      'page1': page, 'page2': page} for page in (3, 5, 3)]
        matches = build([first, second], img_pairs=img_pairs)['matches']
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['pages'], [3, 5])
        self.assertEqual(matches[0]['kind'], 'изображения, стр. 3, 5')

    def test_a_single_shared_page_names_it(self):
        first, second = work('/a.pdf'), work('/b.pdf', name='Сидоров Сидор')
        img_pairs = [{'report1': first['path'], 'report2': second['path'],
                      'page1': 7, 'page2': 7}]
        self.assertEqual(build([first, second], img_pairs=img_pairs)
                         ['matches'][0]['kind'], 'изображение, стр. 7')

    def test_screenshots_of_the_same_interface_are_not_borrowing(self):
        first, second = work('/a.pdf'), work('/b.pdf', name='Сидоров Сидор')
        img_pairs = [{'report1': first['path'], 'report2': second['path'],
                      'page1': 4, 'page2': 4, 'ui_review': True}]
        self.assertEqual(build([first, second], img_pairs=img_pairs)['matches'],
                         [])

    def test_image_duplicates_have_no_percentage(self):
        first, second = work('/a.pdf'), work('/b.pdf', name='Сидоров Сидор')
        img_pairs = [{'report1': first['path'], 'report2': second['path'],
                      'page1': 2, 'page2': 2}]
        self.assertIsNone(build([first, second], img_pairs=img_pairs)
                          ['matches'][0]['pct'])

    def test_matches_are_sorted_by_share_with_image_rows_last(self):
        works = [work(f'/{i}.pdf', name=f'Студент {i}') for i in range(3)]
        pairs = [{'report1': works[0]['path'], 'report2': works[1]['path'],
                  'similarity': 0.3},
                 {'report1': works[0]['path'], 'report2': works[2]['path'],
                  'similarity': 0.9}]
        img_pairs = [{'report1': works[1]['path'], 'report2': works[2]['path'],
                      'page1': 1, 'page2': 1}]
        matches = build(works, pairs=pairs, img_pairs=img_pairs)['matches']
        self.assertEqual([m['pct'] for m in matches], [90, 30, None])

    def test_a_flood_of_matches_is_capped_but_still_counted(self):
        """Полсотни работ с одинаковыми скриншотами дают десятки тысяч
        совпадений, а дайджест уходит в браузер при каждом обновлении списка."""
        total = summary.MAX_MATCHES + 20
        works = [work(f'/{i}.pdf', name=f'Студент {i}') for i in range(total + 1)]
        pairs = [{'report1': works[0]['path'], 'report2': works[i]['path'],
                  'similarity': 0.7} for i in range(1, total + 1)]
        digest = build(works, pairs=pairs)
        self.assertEqual(len(digest['matches']), summary.MAX_MATCHES)
        self.assertEqual(digest['matches_total'], total)


class FeedbackIntegrationTests(unittest.TestCase):
    def test_a_digest_row_feeds_the_ready_made_feedback(self):
        report = work('/a.pdf', gost=criteria(passed=['S1'], failed=['S8']))
        row = build([report])['students'][0]
        text = grading.feedback_text(row, threshold_pct=60)
        self.assertIn('Иванов Иван, ИВТ-201', text)
        self.assertIn('Отсутствует заключение', text)
        self.assertIn('Рекомендуемая оценка за оформление: 50%', text)


if __name__ == '__main__':
    unittest.main()
