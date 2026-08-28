import base64
import zlib
import re
import os
from typing import Optional


def extract_payload(content: str) -> Optional[str]:
    """
    Извлекает строку payload из присваивания вида:
    _p = "..."
    _p = '...'
    """
    match = re.search(r'_p\s*=\s*(["\'])(.*?)\1', content, re.DOTALL)
    if not match:
        return None
    return match.group(2)


def deobfuscate_file(obfuscated_file: str, output_file: str):
    """
    Достает исходный код из замаскированного файла и сохраняет его в чистом виде.
    """
    if not os.path.exists(obfuscated_file):
        print(f"Ошибка: Файл {obfuscated_file} не найден.")
        return
    # Читаем обфусцированный файл
    with open(obfuscated_file, 'r', encoding='utf-8') as f:
        content = f.read()
    # Ищем переменную _p, в которой хранится зашифрованная строка
    encoded_payload = extract_payload(content)
    if not encoded_payload:
        print("Ошибка: Не удалось найти зашифрованную строку payload в файле.")
        return

    try:
        # Раскодируем base64 обратно в сжатые байты
        compressed_data = base64.b64decode(encoded_payload)
        
        # Разжимаем байты алгоритмом zlib
        source_code_bytes = zlib.decompress(compressed_data)
        
        # Декодируем байты в обычный текст (строку)
        source_code = source_code_bytes.decode('utf-8')
        
        # Сохраняем восстановленный код
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(source_code)
            
        print(f"Успех! Исходный код восстановлен и сохранен в файл: {output_file}")
        
    except Exception as e:
        print(f"Произошла ошибка при расшифровке: {e}")
if __name__ == "__main__":
    # 1 аргумент - файл, который мы маскировали ранее
    # 2 аргумент - файл, в который будет сохранен чистый код
    deobfuscate_file("scripts/masked_release_cheker.py", "scripts/release_checker.py")
