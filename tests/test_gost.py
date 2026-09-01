"""Критерии ГОСТ 7.32-2017: каждый отличает верное оформление от неверного.

Устройство тестов описано в tests/fixtures.py: эталонная работа проходит все
22 критерия, а каждый тест портит в ней ровно одну вещь и требует, чтобы
провалился именно сторожащий её критерий.
"""

import unittest

from checker import gost
from checker.gost import check_gost

from . import fixtures


class GostTableTests(unittest.TestCase):
    """GOST_CHECKS объявлена единственным источником правды о наборе проверок:
    по ней рисуются флажки в интерфейсе и раздаются веса оценки. Таблица и сами
    Check() живут в разных местах файла, и разъехаться им ничего не мешает."""

    def setUp(self):
        self.results = check_gost(fixtures.report())

    def test_table_lists_exactly_the_checks_that_run(self):
        self.assertEqual([c['code'] for c in self.results], gost.ALL_CODES)

    def test_names_in_the_table_match_the_checks(self):
        for check in self.results:
            self.assertEqual(check['name'], gost.CHECK_NAMES[check['code']],
                             f'название {check["code"]} разошлось с таблицей')

    def test_every_criterion_has_wording_for_the_feedback(self):
        self.assertEqual(set(gost.FLAW_TEXT), set(gost.ALL_CODES))

    def test_enabled_keeps_only_the_requested_criteria(self):
        results = check_gost(fixtures.report(), enabled={'S1', 'F11'})
        self.assertEqual([c['code'] for c in results], ['S1', 'F11'])

    def test_unknown_code_in_enabled_is_ignored(self):
        results = check_gost(fixtures.report(), enabled={'S1', 'ТАКОГО-НЕТ'})
        self.assertEqual([c['code'] for c in results], ['S1'])

    def test_enabled_none_returns_everything(self):
        self.assertEqual(len(check_gost(fixtures.report(), enabled=None)), 22)


class CriterionTests(unittest.TestCase):
    """Общая часть: эталон проходит всё, порча роняет ровно один критерий."""

    def results(self, report):
        return {c['code']: c for c in check_gost(report)}

    def assertPasses(self, code, report, hint=''):
        check = self.results(report)[code]
        self.assertTrue(check['passed'],
                        f'{code} провалился на исправной работе{hint}: '
                        f'{check["details"]}')

    def assertOnlyFailure(self, code, report, flaw=None):
        """Провалился нужный критерий – и только он.

        Проверка «упал именно этот» важнее, чем «упал хотя бы этот»: порча,
        задевающая соседние критерии, означала бы, что тест ловит не то
        правило, которое называет.
        """
        results = self.results(report)
        failed = sorted(c for c, v in results.items() if not v['passed'])
        self.assertEqual(failed, [code],
                         f'ожидался провал только {code}, провалились {failed}')
        if flaw is not None:
            self.assertEqual(results[code]['flaw'], flaw)


class ReferenceReportTests(CriterionTests):
    def test_reference_report_passes_every_criterion(self):
        failed = [c['code'] for c in check_gost(fixtures.report())
                  if not c['passed']]
        self.assertEqual(failed, [], 'эталонная работа должна проходить всё')


class StructureTests(CriterionTests):
    def test_s1_title_page_without_organisation_and_city(self):
        # Из четырёх обязательных сведений остаются два, и лист перестаёт
        # считаться титульным.
        pages = list(fixtures.PAGES)
        pages[fixtures.TITLE] = 'ОТЧЁТ\nпо учебной практике\nВыполнил Иванов И. И.'
        self.assertOnlyFailure('S1', fixtures.report(pages))

    def test_s2_no_task_sheet(self):
        self.assertOnlyFailure(
            'S2', fixtures.report(fixtures.without(fixtures.TASK),
                                  task_page=None))

    def test_s2_word_task_without_a_sheet_is_only_a_warning(self):
        report = fixtures.report(task_page=None)
        check = self.results(report)['S2']
        self.assertFalse(check['passed'])
        self.assertEqual(check['severity'], 'warning')

    def test_s3_no_abstract(self):
        self.assertOnlyFailure(
            'S3', fixtures.report(fixtures.without(fixtures.ABSTRACT)))

    def test_s3_abstract_without_keywords(self):
        pages = fixtures.replaced(
            'Ключевые слова: ГОСТ, автоматизация, проверка отчётов.',
            'В работе рассмотрена проверка отчётов по стандарту.')
        self.assertOnlyFailure('S3', fixtures.report(pages))

    def test_s4_no_contents(self):
        self.assertOnlyFailure(
            'S4', fixtures.report(fixtures.without(fixtures.CONTENTS)))

    def test_s4_contents_without_page_numbers(self):
        pages = list(fixtures.PAGES)
        pages[fixtures.CONTENTS] = ('4\nСОДЕРЖАНИЕ\n\nРЕФЕРАТ\nВВЕДЕНИЕ\n'
                                    '1 Обзор предметной области')
        self.assertOnlyFailure('S4', fixtures.report(pages))

    def test_s5_introduction_without_relevance(self):
        pages = fixtures.replaced(
            'Актуальность работы обусловлена ростом объёма учебной документации.',
            'Работа посвящена росту объёма учебной документации.')
        self.assertOnlyFailure('S5', fixtures.report(pages))

    def test_s5a_no_goal_and_no_tasks(self):
        pages = fixtures.replaced(
            'Цель работы – автоматизировать проверку отчётов по стандарту.\n'
            'Задачи работы: изучить требования стандарта и реализовать проверку [1].',
            'Далее рассмотрен порядок автоматической проверки отчётов [1].')
        self.assertOnlyFailure('S5A', fixtures.report(pages))

    def test_s6_dot_after_the_chapter_number(self):
        # Точка после номера – отдельная претензия под тем же кодом, поэтому
        # критерий обязан сам назвать замечание вместо общего FLAW_TEXT.
        pages = fixtures.replaced('1 Обзор предметной области\n',
                                  '1. Обзор предметной области\n')
        self.assertOnlyFailure('S6', fixtures.report(pages),
                               flaw='После номера главы стоит точка')

    def test_s7_dot_after_the_subsection_number(self):
        pages = fixtures.replaced('1.1 Постановка задачи\n\nВ таблице',
                                  '1.1. Постановка задачи\n\nВ таблице')
        self.assertOnlyFailure('S7', fixtures.report(pages),
                               flaw='После номера подраздела стоит точка')

    def test_s8_no_conclusion(self):
        self.assertOnlyFailure(
            'S8', fixtures.report(fixtures.without(fixtures.CONCLUSION)))

    def test_s9_too_few_numbered_sources(self):
        pages = fixtures.replaced(
            '3. Петров Б. Б. Обработка документов. Санкт-Петербург, 2024.',
            'Петров Б. Б. Обработка документов. Санкт-Петербург, 2024.')
        self.assertOnlyFailure('S9', fixtures.report(pages))


class FormattingTests(CriterionTests):
    def test_f1_no_page_numbers(self):
        pages = list(fixtures.PAGES)
        pages = [pages[0]] + [
            '\n'.join(line for line in page.split('\n')
                      if not line.strip().isdigit())
            for page in pages[1:]]
        self.assertOnlyFailure('F1', fixtures.report(pages))

    def test_f2_document_set_in_another_typeface(self):
        report = fixtures.report(
            font_info=fixtures.fonts(all_={('Arial', 14.0): 5000}))
        self.assertOnlyFailure('F2', report)

    def test_f3_body_set_at_twelve_points(self):
        report = fixtures.report(
            font_info=fixtures.fonts(body={(fixtures.TNR, 12.0): 4000}))
        self.assertOnlyFailure('F3', report)

    def test_f3_tolerates_rounding_of_the_declared_size(self):
        # Кегль приходит из PDF числом с плавающей точкой; 13.98 – те же 14 пт.
        report = fixtures.report(
            font_info=fixtures.fonts(body={(fixtures.TNR, 13.98): 4000}))
        self.assertPasses('F3', report, ' с кеглем 13.98')

    def test_f4_captions_larger_than_the_body(self):
        report = fixtures.report(
            font_info=fixtures.fonts(aux={(fixtures.TNR, 16.0): 300}))
        self.assertOnlyFailure('F4', report)

    def test_f4_passes_when_there_are_no_captions_at_all(self):
        report = fixtures.report(font_info=fixtures.fonts(aux={}))
        self.assertPasses('F4', report, ' без подписей и таблиц')

    def test_f5_page_numbers_in_another_typeface(self):
        report = fixtures.report(
            font_info=fixtures.fonts(pagenum={('Arial', 14.0): 20}))
        self.assertOnlyFailure('F5', report)

    def test_f6_abbreviated_figure_caption(self):
        pages = fixtures.replaced('Рисунок 1 – Схема работы системы',
                                  'Рис. 1 – Схема работы системы')
        # «Рис. 1» перестаёт быть подписью и становится ссылкой в тексте,
        # поэтому F7 больше нечего искать и он молчит – провал остаётся один.
        self.assertOnlyFailure('F6', fixtures.report(pages))

    def test_f7_figure_never_mentioned_in_the_text(self):
        pages = fixtures.replaced(
            'В таблице 1 перечислены требования, на рисунке 1 показана схема работы.',
            'В таблице 1 перечислены требования к разрабатываемой системе.')
        self.assertOnlyFailure('F7', fixtures.report(pages))

    def test_f8_abbreviated_table_caption(self):
        pages = fixtures.replaced('Таблица 1 – Требования к системе',
                                  'Табл. 1 – Требования к системе')
        self.assertOnlyFailure('F8', fixtures.report(pages))

    def test_f9_dot_at_the_end_of_a_structural_heading(self):
        pages = fixtures.replaced('ЗАКЛЮЧЕНИЕ\n\nВ ходе работы',
                                  'ЗАКЛЮЧЕНИЕ.\n\nВ ходе работы')
        self.assertOnlyFailure('F9', fixtures.report(pages))

    def test_f9_ignores_an_ordinary_sentence_ending_in_a_dot(self):
        self.assertPasses('F9', fixtures.report())

    def test_f10_citations_in_round_brackets(self):
        pages = [page.replace('[1]', '(1)').replace('[2]', '(2)')
                 for page in fixtures.PAGES]
        self.assertOnlyFailure('F10', fixtures.report(pages))

    def test_f11_text_intrudes_into_the_left_margin(self):
        report = fixtures.report(margins_by_page=[
            fixtures.margins(n, left=20) for n in range(1, 9)])
        self.assertOnlyFailure('F11', report)

    def test_f11_measures_the_page_closest_to_the_edge(self):
        """Поле – полоса, куда текст не заходит, поэтому меряется по самой
        «жадной» странице, а не по средней. Иначе глава, кончившаяся на двух
        строках, засчитывалась бы как поле в две трети листа."""
        pages = [fixtures.margins(n, left=30 if n == 5 else 80)
                 for n in range(1, 9)]
        self.assertPasses('F11', fixtures.report(margins_by_page=pages))

    def test_f11_front_matter_is_not_measured(self):
        """Титул и лист задания свёрстаны по своему: текст на них выровнен по
        центру, и левое поле там меряется неверно."""
        pages = [fixtures.margins(1, left=60), fixtures.margins(2, left=60)] + [
            fixtures.margins(n) for n in range(3, 9)]
        self.assertPasses('F11', fixtures.report(margins_by_page=pages))

    def test_f12_structural_heading_not_in_capitals(self):
        pages = fixtures.replaced('5\nВВЕДЕНИЕ\n', '5\nВведение\n')
        self.assertOnlyFailure('F12', fixtures.report(pages))


class DegenerateInputTests(CriterionTests):
    """Работа, которую не удалось разобрать, не должна ронять проверку."""

    def test_empty_report_yields_all_criteria(self):
        results = check_gost({})
        self.assertEqual([c['code'] for c in results], gost.ALL_CODES)

    def test_single_page_document_is_not_faulted_for_numbering(self):
        report = fixtures.report([fixtures.PAGES[fixtures.TITLE]],
                                 task_page=None)
        self.assertPasses('F1', report, ' на одностраничном документе')


if __name__ == '__main__':
    unittest.main()
