# file_collector.py
import os
import sys
from pathlib import Path

def collect_files_to_txt(root_dir=".", output_file="all_files.txt", extensions=None):
    """
    Собирает содержимое всех файлов в один текстовый файл
    
    Args:
        root_dir: Корневая директория для поиска (по умолчанию текущая)
        output_file: Имя выходного файла
        extensions: Список расширений для включения (None = все файлы)
    """
    root_path = Path(root_dir).resolve()
    output_path = Path(output_file)
    
    print(f"📁 Сбор файлов из: {root_path}")
    print(f"📄 Выходной файл: {output_path}")
    
    collected_count = 0
    skipped_count = 0
    
    with open(output_path, 'w', encoding='utf-8') as output:
        # Заголовок
        output.write("=" * 80 + "\n")
        output.write(f"СБОРКА ВСЕХ ФАЙЛОВ ИЗ ДИРЕКТОРИИ\n")
        output.write(f"Директория: {root_path}\n")
        output.write(f"Дата сборки: {os.popen('date /t').read().strip() if os.name == 'nt' else os.popen('date').read().strip()}\n")
        output.write("=" * 80 + "\n\n")
        
        # Рекурсивный обход всех файлов
        for file_path in root_path.rglob('*'):
            if file_path.is_file():
                # Проверка расширения
                if extensions and file_path.suffix.lower() not in extensions:
                    skipped_count += 1
                    continue
                
                # Пропускаем сам выходной файл
                if file_path == output_path:
                    continue
                
                # Пропускаем системные файлы и временные файлы
                if file_path.name.startswith('.') or file_path.name.startswith('~'):
                    continue
                
                try:
                    # Записываем разделитель
                    relative_path = file_path.relative_to(root_path)
                    output.write("\n" + "=" * 80 + "\n")
                    output.write(f"ФАЙЛ: {relative_path}\n")
                    output.write(f"Размер: {file_path.stat().st_size} байт\n")
                    output.write("=" * 80 + "\n\n")
                    
                    # Пытаемся прочитать файл
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                            output.write(content)
                            if not content.endswith('\n'):
                                output.write('\n')
                    except UnicodeDecodeError:
                        # Если не UTF-8, пробуем другие кодировки
                        for encoding in ['cp1251', 'latin-1', 'iso-8859-1']:
                            try:
                                with open(file_path, 'r', encoding=encoding) as f:
                                    content = f.read()
                                    output.write(content)
                                    if not content.endswith('\n'):
                                        output.write('\n')
                                    output.write(f"\n[Примечание: файл прочитан в кодировке {encoding}]\n")
                                break
                            except:
                                continue
                        else:
                            # Если текстовый файл не читается, пропускаем
                            output.write(f"[БИНАРНЫЙ ФАЙЛ - СОДЕРЖИМОЕ НЕ ПОКАЗАНО]\n")
                    
                    collected_count += 1
                    print(f"✓ Добавлен: {relative_path}")
                    
                except Exception as e:
                    output.write(f"[ОШИБКА ЧТЕНИЯ ФАЙЛА: {e}]\n")
                    print(f"✗ Ошибка: {file_path} - {e}")
                    skipped_count += 1
    
    # Статистика в конец файла
    with open(output_path, 'a', encoding='utf-8') as output:
        output.write("\n" + "=" * 80 + "\n")
        output.write("СТАТИСТИКА СБОРКИ\n")
        output.write("=" * 80 + "\n")
        output.write(f"Всего собрано файлов: {collected_count}\n")
        output.write(f"Пропущено файлов: {skipped_count}\n")
        output.write(f"Общий размер выходного файла: {output_path.stat().st_size} байт\n")
    
    print(f"\n✅ Готово! Собрано {collected_count} файлов в {output_file}")
    print(f"📊 Пропущено: {skipped_count} файлов")
    print(f"💾 Размер выходного файла: {output_path.stat().st_size} байт")

def main():
    """Основная функция с обработкой аргументов командной строки"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Сборка всех файлов в директории в один текстовый файл')
    parser.add_argument('-d', '--dir', default='.', help='Корневая директория (по умолчанию текущая)')
    parser.add_argument('-o', '--output', default='all_files.txt', help='Имя выходного файла')
    parser.add_argument('-e', '--extensions', nargs='+', help='Расширения файлов для включения (например: .py .txt .md)')
    
    args = parser.parse_args()
    
    # Преобразуем расширения в список с точками
    extensions = None
    if args.extensions:
        extensions = [ext if ext.startswith('.') else f'.{ext}' for ext in args.extensions]
        print(f"📋 Включаем только файлы с расширениями: {', '.join(extensions)}")
    
    collect_files_to_txt(args.dir, args.output, extensions)

if __name__ == "__main__":
    main()