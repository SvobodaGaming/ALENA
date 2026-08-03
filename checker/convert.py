"""Приведение DOCX/ODT/DOC к PDF через LibreOffice.

Проверка работает только с PDF, и это не ограничение реализации: половина
критериев ГОСТа измеряется по свёрстанной странице — поля меряются по чернилам
букв, номер страницы ищется отдельной строкой в колонтитуле, титульный лист и
лист задания опознаются постранично. У DOCX и ODT страниц нет вовсе, пока
документ не свёрстан, поэтому разбор их XML напрямую дал бы другие вердикты на
той же работе. Формат приводится к PDF здесь — и дальше все форматы идут одним
и тем же кодом.
"""
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

# Форматы, принимаемые наравне с PDF. Расширение — единственный признак:
# содержимое разбирает уже LibreOffice, и гадать за него незачем.
SOURCE_EXTS = ('.docx', '.odt', '.doc')

# Сколько ждать конвертер на одну работу. Битый DOCX не всегда кончается
# ошибкой: LibreOffice случается зависает на нём насмерть, и без предела вся
# партия стояла бы до таймаута gunicorn.
CONVERT_TIMEOUT = 180

NO_CONVERTER = ('Обработка DOCX, ODT и DOC недоступна: '
                'на сервере не установлен LibreOffice')


class ConversionError(Exception):
    """Документ не удалось привести к PDF."""


# Путь к LibreOffice: None — ещё не искали, '' — искали и не нашли.
_EXE = None


def converter() -> str:
    """Путь к LibreOffice или пустая строка, если его нет.

    AU_SOFFICE задаёт бинарник вручную — на серверах, где LibreOffice стоит
    не в PATH. Результат поиска запоминается: на партии в сотню работ обход
    PATH повторялся бы сотню раз впустую.
    """
    global _EXE
    if _EXE is None:
        custom = os.environ.get('AU_SOFFICE', '').strip()
        _EXE = (custom if custom and os.path.exists(custom)
                else shutil.which('soffice') or shutil.which('libreoffice') or '')
    return _EXE


def available() -> bool:
    return bool(converter())


def is_source(name: str) -> bool:
    """Требует ли файл конвертации."""
    return name.lower().endswith(SOURCE_EXTS)


def _tail(out: bytes) -> str:
    """Последняя строка вывода LibreOffice — в ней причина отказа."""
    text = (out or b'').decode('utf-8', 'replace').strip()
    if not text:
        return ''
    return f' ({text.splitlines()[-1][:120]})'


def to_pdf(src: str, work_dir: str, stem: str = '') -> str:
    """Привести документ к PDF и вернуть путь к готовому файлу.

    work_dir — каталог задания. В нём заводится профиль LibreOffice, общий на
    всю партию: с чистого профиля первый запуск идёт заметно дольше, а каталог
    задания и так удаляется в конце проверки.

    Результат кладётся в отдельный подкаталог: LibreOffice называет файл по
    имени исходника, и «Иванов.docx» из партии затёр бы «Иванов.pdf» соседа.
    Имя без расширения задаётся через stem — им проверка разводит совпавшие.
    """
    exe = converter()
    if not exe:
        raise ConversionError(NO_CONVERTER)

    work = Path(work_dir)
    # Профиль задаётся адресом file://, а он обязан быть абсолютным: каталог
    # для временных файлов задаётся настройкой AU_TMP_DIR и вполне может быть
    # записан относительным путём.
    profile = (work / '.soffice').resolve()
    profile.mkdir(parents=True, exist_ok=True)
    out_dir = Path(tempfile.mkdtemp(dir=str(work), prefix='.conv_'))

    cmd = [
        exe, '--headless', '--invisible', '--norestore', '--nolockcheck',
        '--nodefault', '--nofirststartwizard',
        # Свой профиль: без него LibreOffice лезет в домашний каталог, которого
        # под gunicorn может не быть вовсе, и молча выходит с ошибкой. Заодно
        # две проверки, идущие рядом, не дерутся за блокировку одного профиля —
        # каталог задания у каждой свой.
        f'-env:UserInstallation=file://{profile}',
        # Просто «pdf», без явного имени фильтра: LibreOffice сам подберёт его
        # под тип открытого документа, и один и тот же вызов годится и для
        # DOCX, и для ODT, и для старого DOC.
        '--convert-to', 'pdf',
        '--outdir', str(out_dir), src,
    ]

    try:
        # Отдельная сессия процессов: soffice — пусковой скрипт, работу делает
        # порождённый им soffice.bin, и по таймауту убивать надо всю группу,
        # иначе настоящий конвертер переживёт смерть родителя и останется
        # висеть в памяти сервера до перезапуска.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise ConversionError(f'не удалось запустить LibreOffice: {e}')

    try:
        out = proc.communicate(timeout=CONVERT_TIMEOUT)[0]
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        shutil.rmtree(out_dir, ignore_errors=True)
        raise ConversionError(
            f'LibreOffice не уложился в {CONVERT_TIMEOUT} с — '
            'файл повреждён или слишком велик')

    # Судим по результату на диске, а не по коду возврата: LibreOffice выходит
    # с нулём и на документе, который не смог открыть.
    made = sorted(out_dir.glob('*.pdf'))
    if not made:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise ConversionError(
            'LibreOffice не смог прочитать документ' + _tail(out))

    target = out_dir / ((Path(stem).name if stem else Path(src).stem) + '.pdf')
    if made[0] != target:
        made[0].replace(target)
    return str(target)
