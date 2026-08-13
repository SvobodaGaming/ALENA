"""Запись JSON-файлов хранилища одним движением.

Файлы истории, учётных записей и отпечатков читают и пишут одновременно: поток
проверки отмечается в истории, а страница в это же время её опрашивает. Прямая
перезапись файла сначала обрезает его до нуля, и читатель успевает увидеть
обрывок – разобрать его нельзя, и проверка выглядит исчезнувшей. Поэтому пишем
рядом и переименовываем: os.replace подменяет файл целиком, читатель всегда
видит либо старое содержимое, либо новое.
"""

import json
import os
import tempfile
import time
from pathlib import Path

# Сколько раз перечитать файл, если он не разобрался. На обычном диске подмена
# атомарна и повтор не нужен, но общие папки виртуальных машин (prl_fs, vboxsf,
# сетевые диски) показывают читателю пустоту в момент подмены. Без повтора это
# выглядело бы как пропавшая история проверок.
_RETRIES = 3
_RETRY_PAUSE = 0.05


def read_json(path: Path, fallback):
    path = Path(path)
    if not path.exists():
        return fallback
    for attempt in range(_RETRIES):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            if attempt + 1 < _RETRIES:
                time.sleep(_RETRY_PAUSE)
    return fallback


def write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2)
    # Временный файл – в том же каталоге: переименование атомарно только внутри
    # одной файловой системы.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.',
                               suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
