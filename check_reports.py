#!/usr/bin/env python3
"""
Автономная платформа проверки студенческих отчётов.

Проверяет:
  • Заимствование текста (шингловый анализ 5-грамм, порог настраивается)
  • Дублирование изображений (перцептуальный хеш pHash)
  • Соответствие оформления ГОСТ 7.32-2017

Принимаются PDF, DOCX, ODT и DOC. Первые три формата приводятся к PDF через
LibreOffice – дальше проверка идёт одним и тем же кодом, вердикты от формата
не зависят.

Использование:
  python check_reports.py ./папка_с_отчётами
  python check_reports.py ./архив.zip -o отчёт.html
  python check_reports.py ./папка -o отчёт.html --threshold 0.5
"""

import sys
import argparse
import zipfile
import tempfile
import shutil
from pathlib import Path

from checker.convert import SOURCE_EXTS

DOC_EXTS = ('.pdf',) + SOURCE_EXTS


def _check_deps():
    missing = []
    for pkg, import_name in [
        ('pdfplumber', 'pdfplumber'),
        ('PyMuPDF',    'fitz'),
        ('Pillow',     'PIL'),
        ('imagehash',  'imagehash'),
    ]:
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f'[!] Не установлены пакеты: {", ".join(missing)}')
        print(f'    Установите командой:  pip install {" ".join(missing)}')
        sys.exit(1)


def _collect_docs(input_path: str):
    """Return (list_of_doc_paths, tmp_dir_or_None)."""
    p = Path(input_path)
    if not p.exists():
        print(f'[!] Путь не найден: {input_path}', file=sys.stderr)
        sys.exit(1)

    if p.suffix.lower() == '.zip':
        tmp = tempfile.mkdtemp(prefix='report_checker_')
        print(f'    Извлечение архива → {tmp}')
        with zipfile.ZipFile(p, 'r') as z:
            z.extractall(tmp)
        docs, _ = _collect_docs(tmp)
        return docs, tmp

    if p.is_dir():
        return [str(f) for f in sorted(p.rglob('*'))
                if f.is_file() and f.suffix.lower() in DOC_EXTS], None

    if p.suffix.lower() in DOC_EXTS:
        return [str(p)], None

    print('[!] Поддерживаются: папка, .zip архив или файл '
          '.pdf / .docx / .odt / .doc.', file=sys.stderr)
    sys.exit(1)


def _to_pdf(docs: list, work_dir: str) -> list:
    """Привести все форматы к PDF: [(путь к PDF, исходное имя, ошибка)].

    Работа, которая не конвертировалась, из списка не выбрасывается: в отчёт
    она попадает карточкой с ошибкой, иначе о ней негде было бы узнать.
    """
    from checker import convert

    out = []
    used = {Path(d).stem for d in docs if d.lower().endswith('.pdf')}
    for src in docs:
        name = Path(src).name
        if src.lower().endswith('.pdf'):
            out.append((src, name, ''))
            continue
        if not convert.available():
            out.append((src, name, convert.NO_CONVERTER))
            continue
        stem, k = Path(src).stem, 1
        while stem in used:
            k += 1
            stem = f'{Path(src).stem}_{k}'
        used.add(stem)
        print(f'    Конвертация: {name[:70]}')
        try:
            out.append((convert.to_pdf(src, work_dir, stem), name, ''))
        except convert.ConversionError as e:
            print(f'        ⚠ {e}')
            out.append((src, name, str(e)))
    return out


def main():
    parser = argparse.ArgumentParser(
        description='Проверка студенческих отчётов, заимствование и ГОСТ 7.32-2017'
    )
    parser.add_argument('input',
                        help='Папка, zip-архив или отдельный файл '
                             '(.pdf, .docx, .odt, .doc)')
    parser.add_argument('-o', '--output', default='report.html',
                        help='Путь к HTML-отчёту (по умолчанию: report.html)')
    parser.add_argument('--threshold', type=float, default=0.6,
                        help='Порог схожести текста 0-1 (по умолчанию: 0.6 = 60%%)')
    args = parser.parse_args()

    print('='*60)
    print(' Проверка студенческих отчётов')
    print('='*60)
    print('Проверка зависимостей...')
    _check_deps()

    from checker.extractor        import extract_report
    from checker.gost             import check_gost
    from checker.text_plagiarism  import check_text_plagiarism
    from checker.image_plagiarism import check_image_plagiarism
    from checker.reporter         import generate_html_report

    docs, tmp_dir = _collect_docs(args.input)
    if not docs:
        print('[!] Работы не найдены (принимаются .pdf, .docx, .odt, .doc).')
        sys.exit(1)

    print(f'Найдено работ: {len(docs)}')
    print(f'Порог заимствования: {args.threshold:.0%}')
    print()

    # Конвертация пишет во временный каталог, а не рядом с работами: папку
    # преподавателя проверка не трогает.
    work_dir = tempfile.mkdtemp(prefix='report_checker_conv_')
    try:
        # 1. Extraction
        print('[1/4] Извлечение содержимого...')
        pdfs = _to_pdf(docs, work_dir)
        reports = []
        for i, (pdf_path, name, error) in enumerate(pdfs, 1):
            print(f'  {i:>3}/{len(pdfs)}  {name[:70]}')
            report = extract_report(pdf_path, name, error)
            if report.get('error'):
                print(f'        ⚠ Ошибка: {report["error"]}')
            elif report.get('is_scanned'):
                print('        ⚠ Вероятно отсканированный PDF, текст не извлечён')
            reports.append(report)

        # 2. GOST
        print('\n[2/4] Проверка ГОСТ 7.32-2017...')
        for r in reports:
            r['gost_results'] = check_gost(r)

        # 3. Text plagiarism
        print(f'\n[3/4] Анализ заимствования текста ({len(reports)*(len(reports)-1)//2} пар)...')
        text_plag = check_text_plagiarism(reports, threshold=args.threshold)
        flagged = text_plag.get('pairs', [])
        if flagged:
            print(f'  Найдено подозрительных пар: {len(flagged)}')
            for pair in flagged[:5]:
                n1 = Path(pair['report1']).name[:35]
                n2 = Path(pair['report2']).name[:35]
                print(f'  {pair["similarity"]:.0%}  {n1}  ↔  {n2}')
            if len(flagged) > 5:
                print(f'  ... и ещё {len(flagged)-5} пар')
        else:
            print('  Подозрительных пар не найдено.')

        # 4. Image plagiarism
        total_images = sum(len(r.get('images', [])) for r in reports)
        print(f'\n[4/4] Анализ изображений (всего {total_images} шт.)...')
        img_plag = check_image_plagiarism(reports)
        img_pairs = img_plag.get('pairs', [])
        if img_pairs:
            print(f'  Найдено дублей: {len(img_pairs)} пар')
        else:
            print('  Дублей изображений не найдено.')

        # Generate HTML
        print(f'\nГенерация отчёта → {args.output}')
        html = generate_html_report(
            reports=reports,
            historical=[],
            text_plagiarism=text_plag,
            image_plagiarism=img_plag,
            threshold=args.threshold,
        )

        output_path = Path(args.output)
        output_path.write_text(html, encoding='utf-8')

        print()
        print('='*60)
        print(f' Готово! Отчёт: {output_path.absolute()}')
        print(f' Откройте: file://{output_path.absolute()}')
        print('='*60)

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
