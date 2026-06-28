#!/usr/bin/env python3
"""
Автономная платформа проверки студенческих отчётов.

Проверяет:
  • Заимствование текста (шингловый анализ 5-грамм, порог настраивается)
  • Дублирование изображений (перцептуальный хеш pHash)
  • Соответствие оформления ГОСТ 7.32-2017

Использование:
  python check_reports.py ./папка_с_отчётами
  python check_reports.py ./архив.zip -o отчёт.html
  python check_reports.py ./папка -o отчёт.html --threshold 0.5
"""

import sys
import os
import argparse
import zipfile
import tempfile
import shutil
from pathlib import Path


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


def _collect_pdfs(input_path: str):
    """Return (list_of_pdf_paths, tmp_dir_or_None)."""
    p = Path(input_path)
    if not p.exists():
        print(f'[!] Путь не найден: {input_path}', file=sys.stderr)
        sys.exit(1)

    if p.suffix.lower() == '.zip':
        tmp = tempfile.mkdtemp(prefix='report_checker_')
        print(f'    Извлечение архива → {tmp}')
        with zipfile.ZipFile(p, 'r') as z:
            z.extractall(tmp)
        pdfs, _ = _collect_pdfs(tmp)
        return pdfs, tmp

    if p.is_dir():
        seen: set = set()
        result = []
        for pdf in sorted(p.rglob('*.pdf')) + sorted(p.rglob('*.PDF')):
            key = str(pdf.resolve())
            if key not in seen:
                seen.add(key)
                result.append(str(pdf))
        return result, None

    if p.suffix.lower() == '.pdf':
        return [str(p)], None

    print('[!] Поддерживаются: папка, .zip архив или .pdf файл.', file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description='Проверка студенческих отчётов, заимствование и ГОСТ 7.32-2017'
    )
    parser.add_argument('input',
                        help='Папка, zip-архив или отдельный PDF')
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

    pdfs, tmp_dir = _collect_pdfs(args.input)
    if not pdfs:
        print('[!] PDF-файлы не найдены.')
        sys.exit(1)

    print(f'Найдено PDF: {len(pdfs)}')
    print(f'Порог заимствования: {args.threshold:.0%}')
    print()

    tmp = None
    try:
        # 1. Extraction
        print(f'[1/4] Извлечение содержимого...')
        reports = []
        for i, pdf_path in enumerate(pdfs, 1):
            name = Path(pdf_path).name[:70]
            print(f'  {i:>3}/{len(pdfs)}  {name}')
            report = extract_report(pdf_path)
            if report.get('error'):
                print(f'        ⚠ Ошибка: {report["error"]}')
            elif report.get('is_scanned'):
                print(f'        ⚠ Вероятно отсканированный PDF, текст не извлечён')
            reports.append(report)

        # 2. GOST
        print(f'\n[2/4] Проверка ГОСТ 7.32-2017...')
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
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
