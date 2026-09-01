"""Набор тестов проекта. Запуск: ``python -m unittest discover -s tests -t .``

Хранилища переводятся во временный каталог здесь, а не в отдельных тестах:
импорт app.py – это и есть запуск приложения. Он заводит первого
администратора и переписывает settings.json под текущую схему критериев, и
происходит это на импорте тестового модуля, раньше любого setUp. Без подмены
прогон тестов затирал бы настройки рабочей установки, а на чистой машине –
заводил бы там учётные записи и каталог отчётов.

Каталог живёт до конца процесса: history-тесты и тесты хранилищ пишут в него
по-настоящему, а изоляцию друг от друга обеспечивают сами.
"""

import atexit
import tempfile
from pathlib import Path

from checker import accounts, job_store, memory_store, teams

_SANDBOX = tempfile.TemporaryDirectory(prefix='alena_tests_')
atexit.register(_SANDBOX.cleanup)

DATA_DIR = Path(_SANDBOX.name)

accounts.DATA_DIR   = DATA_DIR
accounts.USERS_PATH = DATA_DIR / 'users.json'
accounts.LOG_PATH   = DATA_DIR / 'login_log.json'
accounts.CONF_PATH  = DATA_DIR / 'settings.json'

memory_store.STORE_PATH = DATA_DIR / 'store.json'
teams.STORE_PATH        = DATA_DIR / 'teams.json'
job_store.STORE_PATH    = DATA_DIR / 'jobs.json'
job_store.STORE_DIR     = DATA_DIR / 'jobs'   # рядом с ним лежит и jobs.lock
