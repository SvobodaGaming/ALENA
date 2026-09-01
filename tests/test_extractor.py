"""Разбор PDF: ФИО, группа, лист задания и замер полей.

Опознание студента – самая хрупкая часть разбора: титульные листы свёрстаны у
каждого по-своему, а имена из Moodle приходят служебной строкой. Ошибка здесь
не роняет проверку, а тихо приписывает работу не тому человеку, поэтому
разобранные случаи закреплены тестами по одному.
"""

import tempfile
import unittest
from pathlib import Path

import fitz

from checker import extractor
from checker.extractor import _group_run, _identify_student, _is_task_page
from checker.gost import PT_PER_MM


def name_from(filename='', title=''):
    return _identify_student([title] if title else [], filename)['name']


class TaskSheetTests(unittest.TestCase):
    def test_a_real_task_sheet_is_recognised(self):
        self.assertTrue(_is_task_page(
            'ЗАДАНИЕ\nна учебную практику\n\nСтуденту Иванову И. И.'))

    def test_an_individual_task_heading_counts_too(self):
        self.assertTrue(_is_task_page(
            'Индивидуальное задание на курсовую работу\n\nТема: обработка данных'))

    def test_a_sentence_mentioning_the_task_is_not_a_sheet(self):
        """Раньше хватало любого «задание» среди первых знаков, и обычная фраза
        снимала с листа проверку полей, кегля и номера страницы."""
        self.assertFalse(_is_task_page(
            'ВВЕДЕНИЕ\n\nВ соответствии с индивидуальным заданием на практику '
            'были рассмотрены современные подходы к обработке документов.'))

    def test_a_task_word_without_the_kind_of_work_is_not_a_sheet(self):
        self.assertFalse(_is_task_page('ЗАДАНИЕ\n\nВыполнить расчёт по образцу'))

    def test_an_empty_page_is_not_a_sheet(self):
        self.assertFalse(_is_task_page(''))


class NameFromFilenameTests(unittest.TestCase):
    def test_a_moodle_export_name_is_read_whole(self):
        self.assertEqual(
            name_from('ИВАНОВ ИВАН ИВАНОВИЧ_12345_assignsubmission_file_otchet.pdf'),
            'Иванов Иван Иванович')

    def test_a_moodle_name_without_the_submission_number(self):
        self.assertEqual(
            name_from('Петров Пётр_assignsubmission_file_report.pdf'),
            'Петров Пётр')

    def test_a_compound_name_with_a_particle_keeps_all_four_words(self):
        """«Абдуллаев Али Гасан оглы» приходит из ведомости целиком; на
        титульном листе так не пишут, поэтому четвёртое слово допускается
        только здесь."""
        self.assertEqual(
            name_from('АБДУЛЛАЕВ АЛИ ГАСАН ОГЛЫ_7_assignsubmission_file_x.pdf'),
            'Абдуллаев Али Гасан оглы')

    def test_a_plain_name_with_a_patronymic_is_accepted(self):
        self.assertEqual(name_from('Иванов Иван Иванович.pdf'),
                         'Иванов Иван Иванович')

    def test_a_plain_name_with_initials_is_accepted(self):
        self.assertEqual(name_from('Иванов И.И._ЛР1.pdf'), 'Иванов И.И.')

    def test_a_work_title_is_not_taken_for_a_surname(self):
        """«Основы СКС_ЛР1» уходило в ведомость фамилией."""
        self.assertEqual(name_from('Основы СКС_ЛР1.pdf'), '')

    def test_a_name_without_patronymic_or_initials_waits_for_the_title_page(self):
        self.assertEqual(name_from('Смирнова Елена_ЛР1.pdf'), '')

    def test_a_double_surname_keeps_its_hyphen(self):
        self.assertEqual(name_from('Иванова-Петрова Анна Сергеевна.pdf'),
                         'Иванова-Петрова Анна Сергеевна')


class NameFromTitlePageTests(unittest.TestCase):
    def test_the_name_follows_the_word_performed_by(self):
        self.assertEqual(name_from(title='Выполнил: Иванов И.И.'),
                         'Иванов И.И.')

    def test_the_name_may_stand_on_the_next_line(self):
        self.assertEqual(
            name_from(title='Выполнил студент группы ИВТ-201\nПетров Пётр Петрович'),
            'Петров Пётр Петрович')

    def test_capitals_are_brought_back_to_ordinary_case(self):
        self.assertEqual(name_from(title='Выполнила: АРТАМОНОВА ОЛЬГА'),
                         'Артамонова Ольга')

    def test_the_university_header_is_not_a_person(self):
        """Разбор шапки регулярно выдавал за студента «Минобрнауки России»."""
        header = ('МИНОБРНАУКИ РОССИИ\n'
                  'Федеральное государственное бюджетное образовательное '
                  'учреждение высшего образования')
        self.assertEqual(name_from(title=header), '')

    def test_the_filename_wins_over_the_title_page(self):
        """Ведомость называет работы строго, титульный лист – как получится."""
        student = _identify_student(['Выполнил: Петров П.П.'],
                                    'Иванов Иван Иванович.pdf')
        self.assertEqual(student['name'], 'Иванов Иван Иванович')


class GroupTests(unittest.TestCase):
    def group_from(self, filename='', title=''):
        return _identify_student([title] if title else [], filename)['group']

    def test_a_hyphenated_group_is_read_from_the_title_page(self):
        self.assertEqual(self.group_from(title='студент группы ИВТ-20-01'),
                         'ИВТ-20-01')

    def test_a_group_is_read_from_the_filename_when_the_title_is_silent(self):
        self.assertEqual(self.group_from(filename='Козлов_КА-22-06_ЛР1.pdf'),
                         'КА-22-06')

    def test_a_run_together_group_in_the_filename_is_split(self):
        """Подчёркивание входит в \\w, поэтому границу кода приходится задавать
        вручную: «Козлов_КА2206_ЛР1»."""
        self.assertEqual(self.group_from(filename='Козлов_КА2206_ЛР1.pdf'),
                         'КА-22-06')

    def test_a_year_is_not_a_group(self):
        self.assertEqual(_group_run('СКС2024'), '')
        self.assertEqual(self.group_from(filename='Отчет_2025.pdf'), '')

    def test_a_run_together_group_on_the_title_page_needs_the_word_group(self):
        """Без слова «группа» в «КА2206» превращается любое четырёхзначное
        число с буквами."""
        self.assertEqual(self.group_from(title='группа КА2206'), 'КА-22-06')
        self.assertEqual(self.group_from(title='вариант КА2206'), '')


class TitlePageFactsTests(unittest.TestCase):
    TITLE = ('МИНОБРНАУКИ РОССИИ\n'
             'Федеральное государственное бюджетное образовательное учреждение '
             'высшего образования «Уральский государственный университет»\n'
             'Лабораторная работа № 3\n'
             'Выполнил студент группы ИВТ-20-01\n'
             'Иванов Иван Иванович\n'
             'Москва, 2025')

    def setUp(self):
        self.student = _identify_student([self.TITLE], 'otchet.pdf')

    def test_the_organisation_is_picked_up(self):
        org = self.student['org']
        self.assertTrue(org.startswith('Федеральное государственное'), org)
        self.assertLessEqual(len(org), 120, 'строка обрезается для показа')

    def test_the_year_is_picked_up(self):
        self.assertEqual(self.student['year'], '2025')

    def test_the_kind_of_work_is_picked_up(self):
        self.assertEqual(self.student['work_title'], 'Лабораторная работа № 3')

    def test_a_year_typed_into_a_ruled_blank_is_still_read(self):
        """Из PDF бланк «Москва, 20__» выходит как «Москва, 20 2 5»."""
        student = _identify_student(['Москва, 20 2 5'], 'x.pdf')
        self.assertEqual(student['year'], '2025')


class PdfRoundTripTests(unittest.TestCase):
    """Один настоящий PDF от начала до конца: разбор больше нигде не проверяется
    на файле, и молчаливая поломка pdfplumber иначе осталась бы незамеченной."""

    LEFT, TOP = 30 * PT_PER_MM, 20 * PT_PER_MM
    W, H = 595.28, 841.89

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.path = str(Path(cls._dir.name) / 'report.pdf')
        doc = fitz.open()
        for number in (1, 2):
            page = doc.new_page(width=cls.W, height=cls.H)
            offset = cls.TOP + 14
            for line in (f'Page {number} heading',
                         'Ordinary body line of the report.'):
                page.insert_text((cls.LEFT, offset), line,
                                 fontname='tiro', fontsize=14)
                offset += 20
            page.insert_text((cls.W / 2, cls.H - cls.TOP), str(number),
                             fontname='tiro', fontsize=14)
        doc.save(cls.path)
        doc.close()

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def setUp(self):
        self.report = extractor.extract_report(
            self.path, filename='Иванов Иван Иванович.pdf')

    def test_every_page_is_read(self):
        self.assertEqual(self.report['pages_count'], 2)
        self.assertIn('Ordinary body line', self.report['text_by_page'][0])

    def test_the_typeface_and_size_reach_the_font_counters(self):
        names = {name for name, _ in self.report['font_info']['all']}
        self.assertTrue(any('times' in name.lower() for name in names), names)
        self.assertEqual({size for _, size in self.report['font_info']['body']},
                         {14.0})

    def test_the_page_number_is_counted_separately_from_the_body(self):
        """F5 проверяет гарнитуру номеров страниц, и для этого они должны
        попасть в свой счётчик, а не в общий."""
        self.assertTrue(self.report['font_info']['pagenum'])

    def test_margins_are_measured_from_the_ink_of_the_glyphs(self):
        page = self.report['margins_by_page'][0]
        self.assertAlmostEqual(page['x0'] / PT_PER_MM, 30, delta=1)
        self.assertAlmostEqual(page['top'] / PT_PER_MM, 20, delta=6)

    def test_the_student_is_identified_from_the_filename(self):
        self.assertEqual(self.report['student']['name'], 'Иванов Иван Иванович')

    def test_a_file_that_failed_earlier_keeps_its_card(self):
        """Преподаватель должен видеть, чья работа не прошла, а не молча
        недосчитаться её в ведомости."""
        report = extractor.extract_report(
            self.path, filename='Иванов Иван Иванович.pdf',
            error='не удалось конвертировать')
        self.assertEqual(report['error'], 'не удалось конвертировать')
        self.assertEqual(report['pages_count'], 0)
        self.assertEqual(report['student']['name'], 'Иванов Иван Иванович')

    def test_a_file_that_is_not_a_pdf_does_not_raise(self):
        broken = Path(self._dir.name) / 'broken.pdf'
        broken.write_bytes(b'not a pdf at all')
        report = extractor.extract_report(str(broken), filename='broken.pdf')
        self.assertEqual(report['pages_count'], 0)


if __name__ == '__main__':
    unittest.main()
