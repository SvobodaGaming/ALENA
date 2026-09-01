"""Рекомендуемая оценка за оформление и готовый отзыв студенту."""

import unittest

from checker import grading
from checker.gost import check_gost

from . import fixtures


def results(passed_codes=(), failed_codes=(), flaw=''):
    """Список критериев в том виде, в каком его отдаёт check_gost."""
    out = [{'code': code, 'name': code, 'passed': True, 'details': '',
            'severity': 'error', 'flaw': ''} for code in passed_codes]
    out += [{'code': code, 'name': code, 'passed': False, 'details': 'подробности',
             'severity': 'error', 'flaw': flaw} for code in failed_codes]
    return out


class CleanWeightsTests(unittest.TestCase):
    def test_reads_the_compact_string_form_used_by_the_api(self):
        self.assertEqual(grading.clean_weights('S1:50,F2:20'),
                         {'S1': 50, 'F2': 20})

    def test_semicolons_separate_pairs_as_well(self):
        self.assertEqual(grading.clean_weights('S1:50;F2:20'),
                         {'S1': 50, 'F2': 20})

    def test_lowercase_codes_are_accepted(self):
        self.assertEqual(grading.clean_weights({'s5a': 30}), {'S5A': 30})

    def test_values_are_clamped_to_the_allowed_range(self):
        self.assertEqual(grading.clean_weights({'S1': -5, 'F2': 500}),
                         {'S1': 0, 'F2': 100})

    def test_unknown_codes_and_junk_values_are_dropped(self):
        self.assertEqual(
            grading.clean_weights({'S1': 40, 'XX': 40, 'F2': 'много'}),
            {'S1': 40})

    def test_empty_input_gives_empty_weights(self):
        for value in (None, '', {}, []):
            self.assertEqual(grading.clean_weights(value), {})


class CleanScaleTests(unittest.TestCase):
    def test_keeps_a_scale_inside_the_range(self):
        self.assertEqual(grading.clean_scale('5'), 5)

    def test_falls_back_to_percent_outside_the_range(self):
        for value in (1, 101, -3):
            self.assertEqual(grading.clean_scale(value), grading.DEFAULT_SCALE)

    def test_falls_back_to_percent_on_junk(self):
        for value in (None, '', 'пять'):
            self.assertEqual(grading.clean_scale(value), grading.DEFAULT_SCALE)


class SharesTests(unittest.TestCase):
    def test_selected_criteria_always_add_up_to_a_hundred(self):
        share = grading.shares({'S1': 30, 'F2': 10}, ['S1', 'F2'])
        self.assertAlmostEqual(sum(share.values()), 100)
        self.assertAlmostEqual(share['S1'], 75)

    def test_criteria_without_a_weight_are_worth_the_default(self):
        share = grading.shares({}, ['S1', 'F2', 'F3', 'F4'])
        self.assertEqual(set(share.values()), {25})

    def test_all_weights_zero_falls_back_to_equal_criteria(self):
        """Вырожденный случай: без запасного варианта оценка была бы
        неопределённой при любом результате проверки."""
        share = grading.shares({'S1': 0, 'F2': 0}, ['S1', 'F2'])
        self.assertEqual(share, {'S1': 50.0, 'F2': 50.0})

    def test_no_criteria_selected_gives_no_shares(self):
        self.assertEqual(grading.shares({'S1': 50}, []), {})


class GradeTests(unittest.TestCase):
    def test_all_criteria_passed_is_a_hundred_percent(self):
        self.assertEqual(grading.grade(results(passed_codes=['S1', 'F2']))['pct'],
                         100)

    def test_all_criteria_failed_is_zero(self):
        self.assertEqual(grading.grade(results(failed_codes=['S1', 'F2']))['pct'],
                         0)

    def test_weight_decides_how_much_a_criterion_costs(self):
        grade = grading.grade(results(passed_codes=['S1'], failed_codes=['F2']),
                              weights={'S1': 90, 'F2': 10})
        self.assertEqual(grade['pct'], 90)

    def test_a_criterion_weighted_zero_is_still_checked_but_costs_nothing(self):
        """Обещание README: критерий с весом 0 проверяется и попадает в отзыв,
        он просто не двигает оценку."""
        gost = results(passed_codes=['S1'], failed_codes=['F2'])
        grade = grading.grade(gost, weights={'S1': 100, 'F2': 0})
        self.assertEqual(grade['pct'], 100)
        self.assertEqual([f['code'] for f in grading.flaws(gost)], ['F2'])

    def test_lost_criteria_are_listed_from_the_dearest_down(self):
        grade = grading.grade(results(failed_codes=['S1', 'F2', 'F3']),
                              weights={'S1': 10, 'F2': 80, 'F3': 30})
        self.assertEqual([item['code'] for item in grade['lost']],
                         ['F2', 'F3', 'S1'])

    def test_score_is_the_percentage_on_the_chosen_scale(self):
        grade = grading.grade(results(passed_codes=['S1'], failed_codes=['F2']),
                              scale=5)
        self.assertEqual(grade['pct'], 50)
        self.assertEqual(grade['score'], 2.5)
        self.assertEqual(grade['scale'], 5)

    def test_percentage_scale_needs_no_separate_score(self):
        self.assertIsNone(grading.grade(results(passed_codes=['S1']))['score'])

    def test_a_work_without_results_has_no_grade(self):
        grade = grading.grade([])
        self.assertIsNone(grade['pct'])
        self.assertEqual(grade['criteria'], 0)

    def test_the_reference_report_earns_full_marks(self):
        grade = grading.grade(check_gost(fixtures.report()))
        self.assertEqual(grade['pct'], 100)
        self.assertEqual(grade['lost'], [])


class FlawsTests(unittest.TestCase):
    def test_only_failed_criteria_become_remarks(self):
        flaws = grading.flaws(results(passed_codes=['S1'], failed_codes=['F2']))
        self.assertEqual([f['code'] for f in flaws], ['F2'])

    def test_remarks_keep_the_order_of_the_gost_table(self):
        gost = check_gost(fixtures.report(fixtures.without(
            fixtures.ABSTRACT, fixtures.CONCLUSION)))
        codes = [f['code'] for f in grading.flaws(gost)]
        self.assertEqual(codes, sorted(codes, key=[c['code'] for c in gost].index))

    def test_a_criterion_that_names_its_own_remark_wins_over_the_table(self):
        """S6 проваливается двумя разными способами, и общий заголовок
        «работа не разбита на главы» описывал бы второй неверно."""
        flaws = grading.flaws(results(failed_codes=['S6'],
                                      flaw='После номера главы стоит точка'))
        self.assertEqual(flaws[0]['text'], 'После номера главы стоит точка')

    def test_without_its_own_wording_the_table_text_is_used(self):
        flaws = grading.flaws(results(failed_codes=['S6']))
        self.assertEqual(flaws[0]['text'],
                         'Работа не разбита на нумерованные главы')


class FeedbackTests(unittest.TestCase):
    def student(self, **overrides):
        data = {
            'fio': 'Иванов Иван', 'group': 'ИВТ-201', 'gost': 100,
            'plag': 10, 'no_text': False, 'fails': [], 'flaws': [],
            'grade': grading.grade(results(passed_codes=['S1'])), 'error': None,
        }
        data.update(overrides)
        return data

    def test_a_clean_work_says_so_and_still_shows_the_grade(self):
        text = grading.feedback_text(self.student())
        self.assertIn('Иванов Иван, ИВТ-201', text)
        self.assertIn('Замечаний по оформлению нет.', text)
        self.assertIn('Рекомендуемая оценка за оформление: 100%', text)

    def test_remarks_are_listed_under_a_bullet(self):
        student = self.student(flaws=grading.flaws(results(failed_codes=['S8'])))
        text = grading.feedback_text(student)
        self.assertIn('• Отсутствует заключение', text)

    def test_details_are_added_only_when_asked(self):
        student = self.student(flaws=grading.flaws(results(failed_codes=['S8'])))
        self.assertNotIn('подробности', grading.feedback_text(student))
        self.assertIn('подробности', grading.feedback_text(student, details=True))

    def test_borrowing_at_or_above_the_threshold_is_reported(self):
        text = grading.feedback_text(self.student(plag=70), threshold_pct=60)
        self.assertIn('Совпадение с другой работой – 70%', text)

    def test_borrowing_below_the_threshold_is_not_mentioned(self):
        text = grading.feedback_text(self.student(plag=20), threshold_pct=60)
        self.assertNotIn('Совпадение', text)

    def test_an_unreadable_work_gets_no_grade_at_all(self):
        """Оценка за оформление у скана не «100 %», а неизвестна."""
        text = grading.feedback_text(self.student(no_text=True, plag=None))
        self.assertIn('Текст из файла не извлекается', text)
        self.assertNotIn('Рекомендуемая оценка', text)

    def test_a_broken_file_reports_the_error_instead_of_remarks(self):
        lines = grading.feedback_lines(self.student(error='PDF не открылся'))
        self.assertEqual(lines, ['Файл не удалось прочитать: PDF не открылся'])

    def test_the_score_is_shown_next_to_the_percentage(self):
        student = self.student(
            grade=grading.grade(results(passed_codes=['S1']), scale=5))
        self.assertIn('(5 из 5)', grading.feedback_text(student))


if __name__ == '__main__':
    unittest.main()
