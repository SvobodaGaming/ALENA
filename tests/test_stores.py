"""Хранилища: атомарная запись JSON, база отпечатков, группы преподавателей.

Все тесты идут по JSON-ветке с подменённым путём: настоящий memory/ трогать
нельзя, а ветка PostgreSQL требует живой базы и проверяется миграцией.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from checker import db, jsonstore, memory_store, teams


class TempStore(unittest.TestCase):
    """Хранилище в отдельном каталоге, база выключена."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        root = Path(self._dir.name)
        self.addCleanup(self._dir.cleanup)
        patches = [
            patch.object(db, 'DB_ENABLED', False),
            patch.object(memory_store, 'STORE_PATH', root / 'store.json'),
            patch.object(teams, 'STORE_PATH', root / 'teams.json'),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.root = root


class JsonStoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / 'nested' / 'data.json'

    def test_written_value_reads_back_unchanged(self):
        jsonstore.write_json(self.path, {'ключ': ['значение', 1]})
        self.assertEqual(jsonstore.read_json(self.path, None),
                         {'ключ': ['значение', 1]})

    def test_missing_file_gives_the_fallback(self):
        self.assertEqual(jsonstore.read_json(self.path, {'по': 'умолчанию'}),
                         {'по': 'умолчанию'})

    def test_unreadable_file_gives_the_fallback_instead_of_raising(self):
        """Оборвавшаяся запись не должна выглядеть как крах приложения."""
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{это не JSON', encoding='utf-8')
        self.assertEqual(jsonstore.read_json(self.path, {}), {})

    def test_cyrillic_is_stored_as_letters_not_escapes(self):
        jsonstore.write_json(self.path, {'группа': 'ИВТ-201'})
        self.assertIn('ИВТ-201', self.path.read_text(encoding='utf-8'))

    def test_no_temporary_files_are_left_next_to_the_store(self):
        """Пишем рядом и переименовываем; хвосты в каталоге означали бы, что
        подмена не доведена до конца."""
        jsonstore.write_json(self.path, {'a': 1})
        jsonstore.write_json(self.path, {'a': 2})
        self.assertEqual([p.name for p in self.path.parent.iterdir()],
                         ['data.json'])

    def test_a_failed_write_leaves_the_previous_content_intact(self):
        jsonstore.write_json(self.path, {'a': 1})
        with self.assertRaises(TypeError):
            jsonstore.write_json(self.path, {'a': object()})
        self.assertEqual(jsonstore.read_json(self.path, None), {'a': 1})
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)


class StudentIdentityTests(unittest.TestCase):
    def test_identity_is_name_and_group_in_lower_case(self):
        self.assertEqual(
            memory_store.student_id({'name': 'Иванов И. И.', 'group': 'ИВТ-201'}),
            'иванов и. и.|ивт-201')

    def test_a_group_alone_does_not_identify_a_person(self):
        """Иначе все работы группы считались бы работами одного студента и не
        сравнивались бы между собой."""
        self.assertEqual(memory_store.student_id({'group': 'ИВТ-201'}),
                         memory_store.NO_STUDENT)

    def test_an_empty_student_is_anonymous(self):
        self.assertEqual(memory_store.student_id({}), memory_store.NO_STUDENT)
        self.assertEqual(memory_store.student_id(None), memory_store.NO_STUDENT)


class FingerprintStoreTests(TempStore):
    def work(self, name='Иванов Иван', group='ИВТ-201', text='слово ' * 60):
        return {'filename': f'{name}.pdf', 'student': {'name': name, 'group': group},
                'full_text': text, 'images': [], 'pages_count': 5}

    def test_versions_are_handed_out_in_order(self):
        self.assertEqual(memory_store.add_reports([self.work()], 'job1', 'teacher'),
                         [1])
        self.assertEqual(memory_store.add_reports([self.work()], 'job2', 'teacher'),
                         [2])

    def test_a_batch_numbers_its_own_repeats(self):
        versions = memory_store.add_reports(
            [self.work(), self.work()], 'job1', 'teacher')
        self.assertEqual(versions, [1, 2])

    def test_two_teachers_keep_separate_numbering(self):
        self.assertEqual(memory_store.add_reports([self.work()], 'j', 'anna'), [1])
        self.assertEqual(memory_store.add_reports([self.work()], 'j', 'boris'), [1])

    def test_a_teacher_sees_only_their_own_fingerprints(self):
        memory_store.add_reports([self.work()], 'j', 'anna')
        memory_store.add_reports([self.work(name='Петров Пётр')], 'j', 'boris')
        self.assertEqual(len(memory_store.load_store('anna')), 1)
        self.assertEqual(len(memory_store.load_store()), 2)

    def test_a_group_member_sees_the_shared_base(self):
        memory_store.add_reports([self.work()], 'j', 'anna')
        memory_store.add_reports([self.work(name='Петров Пётр')], 'j', 'boris')
        self.assertEqual(len(memory_store.load_store(['anna', 'boris'])), 2)

    def test_clearing_one_teacher_leaves_the_other_untouched(self):
        memory_store.add_reports([self.work()], 'j', 'anna')
        memory_store.add_reports([self.work(name='Петров Пётр')], 'j', 'boris')
        self.assertEqual(memory_store.clear_store('anna'), 1)
        self.assertEqual(len(memory_store.load_store('boris')), 1)

    def test_clearing_without_an_owner_wipes_the_whole_base(self):
        memory_store.add_reports([self.work()], 'j', 'anna')
        memory_store.add_reports([self.work(name='Петров Пётр')], 'j', 'boris')
        self.assertEqual(memory_store.clear_store(), 2)
        self.assertEqual(memory_store.load_store(), {})

    def test_a_single_entry_can_be_deleted_by_key(self):
        memory_store.add_reports([self.work()], 'j', 'anna')
        key = next(iter(memory_store.load_store()))
        self.assertTrue(memory_store.delete_entry(key))
        self.assertFalse(memory_store.delete_entry(key))

    def test_saving_one_slice_does_not_disturb_another(self):
        memory_store.add_reports([self.work()], 'j', 'anna')
        memory_store.add_reports([self.work(name='Петров Пётр')], 'j', 'boris')
        anna = memory_store.load_store('anna')
        for entry in anna.values():
            entry['pages_count'] = 42
        memory_store.save_store(anna)
        self.assertEqual(len(memory_store.load_store()), 2)

    def test_the_stored_text_is_normalized_not_raw(self):
        """В базе лежит только нормализованный текст и его хеш: по ним ищется
        заимствование, а исходный текст работы хранить незачем."""
        memory_store.add_reports([self.work(text='Привет,   МИР! 42')], 'j', 'a')
        entry = next(iter(memory_store.load_store().values()))
        self.assertEqual(entry['normalized_text'], 'привет мир')
        self.assertTrue(entry['text_hash'])
        self.assertNotIn('full_text', entry)

    def test_an_empty_batch_writes_nothing(self):
        self.assertEqual(memory_store.add_reports([], 'j', 'anna'), [])
        self.assertEqual(memory_store.load_store(), {})

    def test_the_summary_is_newest_first(self):
        store = {
            'a|v1': {'added_at': '01.09.2025 10:00', 'version': 1},
            'b|v1': {'added_at': '15.12.2025 09:00', 'version': 1},
            'c|v1': {'added_at': '02.09.2025 10:00', 'version': 1},
        }
        rows = memory_store.get_summary(store)
        self.assertEqual([r['key'] for r in rows], ['b|v1', 'c|v1', 'a|v1'])

    def test_the_summary_survives_a_broken_timestamp(self):
        rows = memory_store.get_summary({'a|v1': {'added_at': 'когда-то'}})
        self.assertEqual(rows[0]['image_count'], 0)

    def test_a_stored_entry_becomes_a_report_the_checkers_understand(self):
        memory_store.add_reports([self.work()], 'job7', 'anna')
        key, entry = next(iter(memory_store.load_store().items()))
        report = memory_store.to_virtual_report(key, entry)
        self.assertEqual(report['path'], f'memory://{key}')
        self.assertTrue(report['is_historical'])
        self.assertEqual(report['full_text'], '')
        self.assertEqual(report['normalized_text'], entry['normalized_text'])

    def test_a_virtual_report_drops_image_records_without_hashes(self):
        entry = {'image_data': [{'page': 1, 'hashes': []},
                                {'page': 2, 'hashes': ['ff00']},
                                'мусор'],
                 'normalized_text': 'текст'}
        report = memory_store.to_virtual_report('k', entry)
        self.assertEqual([i['page'] for i in report['precomputed_images']], [2])


class TeamTests(TempStore):
    def test_a_new_team_is_readable_back(self):
        team = teams.create_team('Кафедра ИВТ', ['anna', 'boris'])
        self.assertEqual(teams.get_team(team['team_id'])['members'],
                         ['anna', 'boris'])

    def test_duplicate_logins_do_not_reach_the_roster(self):
        team = teams.create_team('Кафедра', ['anna', ' anna ', '', 'boris'])
        self.assertEqual(team['members'], ['anna', 'boris'])

    def test_a_long_name_is_trimmed(self):
        team = teams.create_team('и' * 200)
        self.assertEqual(len(team['name']), teams.NAME_MAX)

    def test_an_unknown_team_is_simply_absent(self):
        self.assertIsNone(teams.get_team('нет-такой'))
        self.assertIsNone(teams.get_team(''))

    def test_deleting_a_team_reports_whether_it_existed(self):
        team = teams.create_team('Кафедра', ['anna'])
        self.assertTrue(teams.delete_team(team['team_id']))
        self.assertFalse(teams.delete_team(team['team_id']))

    def test_a_teacher_outside_any_group_sees_only_themselves(self):
        self.assertEqual(teams.visible_owners('anna'), ['anna'])
        self.assertEqual(teams.colleagues('anna'), [])

    def test_a_group_member_sees_their_colleagues(self):
        teams.create_team('Кафедра', ['anna', 'boris'])
        self.assertEqual(teams.visible_owners('anna'), ['anna', 'boris'])
        self.assertEqual(teams.colleagues('anna'), ['boris'])

    def test_several_groups_merge_into_one_visible_base(self):
        teams.create_team('Первая', ['anna', 'boris'])
        teams.create_team('Вторая', ['anna', 'vera'])
        self.assertEqual(teams.visible_owners('anna'), ['anna', 'boris', 'vera'])

    def test_teams_of_are_listed_alphabetically(self):
        teams.create_team('Ядро', ['anna'])
        teams.create_team('Базы', ['anna'])
        self.assertEqual([t['name'] for t in teams.teams_of('anna')],
                         ['Базы', 'Ядро'])

    def test_a_deleted_account_is_dropped_from_every_group(self):
        """Логин без учётной записи нельзя ни открыть, ни убрать иначе как
        правкой хранилища, а заведённый заново тёзка молча получил бы доступ к
        чужой базе."""
        teams.create_team('Первая', ['anna', 'boris'])
        teams.create_team('Вторая', ['boris', 'vera'])
        teams.create_team('Третья', ['anna'])
        self.assertEqual(teams.drop_member('boris'), 2)
        self.assertEqual(teams.visible_owners('anna'), ['anna'])

    def test_disbanding_a_group_deletes_no_fingerprints(self):
        memory_store.add_reports(
            [{'filename': 'a.pdf', 'student': {'name': 'Иванов', 'group': 'ИВТ'},
              'full_text': 'слово ' * 60, 'images': [], 'pages_count': 3}],
            'j', 'anna')
        team = teams.create_team('Кафедра', ['anna', 'boris'])
        teams.delete_team(team['team_id'])
        self.assertEqual(len(memory_store.load_store('anna')), 1)

    def test_the_roster_survives_a_round_trip_through_the_file(self):
        team = teams.create_team('Кафедра', ['anna'])
        team['members'] = ['anna', 'boris']
        teams.save_team(team)
        raw = json.loads(teams.STORE_PATH.read_text(encoding='utf-8'))
        self.assertEqual(raw[team['team_id']]['members'], ['anna', 'boris'])


if __name__ == '__main__':
    unittest.main()
