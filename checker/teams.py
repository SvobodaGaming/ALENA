"""Группы преподавателей — общая база отпечатков на несколько учётных записей.

Кафедра ведёт один курс вдвоём-втроём, и работа, сданная одному преподавателю,
для второго остаётся невидимой: база отпечатков у каждого своя. Группа снимает
ровно эту стену и только её — поиск заимствований идёт по объединённой базе
участников, а проверки, отчёты и история остаются личными.

Состав хранится в самой группе, а не в учётной записи: преподаватель состоит в
скольких угодно группах, и добавление участника не трогает таблицу `users` —
её столбцы перечислены поимённо в трёх местах (db.USER_COLS, дамп, загрузка
дампа), и лишнее поле пришлось бы проводить через все.

Backed by PostgreSQL when DATABASE_URL is set, otherwise by a JSON file next to
the fingerprint store (same fallback strategy as checker/memory_store.py).
"""

import threading
import uuid
from datetime import datetime
from pathlib import Path

from checker import db, jsonstore

STORE_PATH = Path(__file__).parent.parent / 'memory' / 'teams.json'
_lock = threading.Lock()

NAME_MAX = 80


def _read_all() -> dict:
    return jsonstore.read_json(STORE_PATH, {})


def load_teams() -> dict:
    """Все группы: {team_id: {team_id, name, members, created_at}}."""
    if db.DB_ENABLED:
        return db.teams_load_all()
    return _read_all()


def get_team(team_id: str):
    if not team_id:
        return None
    return load_teams().get(team_id)


def save_team(team: dict) -> None:
    if db.DB_ENABLED:
        db.teams_save(team)
        return
    with _lock:
        teams = _read_all()
        teams[team['team_id']] = team
        jsonstore.write_json(STORE_PATH, teams)


def delete_team(team_id: str) -> bool:
    if db.DB_ENABLED:
        return db.teams_delete(team_id)
    with _lock:
        teams = _read_all()
        if team_id not in teams:
            return False
        del teams[team_id]
        jsonstore.write_json(STORE_PATH, teams)
    return True


def create_team(name: str, members=()) -> dict:
    team = {
        'team_id':    uuid.uuid4().hex[:12],
        'name':       name.strip()[:NAME_MAX],
        'members':    _clean_members(members),
        'created_at': datetime.now().strftime('%d.%m.%Y %H:%M'),
    }
    save_team(team)
    return team


def set_members(team_id: str, members) -> bool:
    team = get_team(team_id)
    if team is None:
        return False
    team['members'] = _clean_members(members)
    save_team(team)
    return True


def rename(team_id: str, name: str) -> bool:
    team = get_team(team_id)
    if team is None or not name.strip():
        return False
    team['name'] = name.strip()[:NAME_MAX]
    save_team(team)
    return True


def drop_member(login: str) -> int:
    """Убрать логин из всех групп. Возвращает число затронутых групп.

    Нужно удалению учётной записи: логин без записи оставлял бы в составе
    строку, которую нельзя ни открыть, ни убрать иначе как правкой хранилища,
    а после заведения тёзки с тем же логином она молча вернула бы человеку
    доступ к чужой базе.
    """
    touched = 0
    for team in load_teams().values():
        if login in (team.get('members') or []):
            team['members'] = [m for m in team['members'] if m != login]
            save_team(team)
            touched += 1
    return touched


def _clean_members(members) -> list:
    """Логины без повторов, в исходном порядке."""
    seen, out = set(), []
    for login in members or []:
        login = (login or '').strip()
        if login and login not in seen:
            seen.add(login)
            out.append(login)
    return out


# Кто кого видит

def teams_of(login: str) -> list:
    """Группы, в которых состоит учётная запись, по алфавиту."""
    if not login:
        return []
    mine = [t for t in load_teams().values() if login in (t.get('members') or [])]
    return sorted(mine, key=lambda t: t.get('name', ''))


def visible_owners(login: str) -> list:
    """Владельцы отпечатков, доступных этой учётной записи.

    Всегда содержит сам логин: без группы список равен `[login]`, и вызывающий
    получает ровно прежнюю личную базу.
    """
    owners = {login}
    for team in teams_of(login):
        owners.update(team.get('members') or [])
    return sorted(owners)


def colleagues(login: str) -> list:
    """Соучастники по группам, без самого логина."""
    return [o for o in visible_owners(login) if o != login]
