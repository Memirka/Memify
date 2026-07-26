import re
import os

def normalize_track_url(url: str) -> str:
    """Normalize track URLs by stripping domain and leading slashes."""
    if not url:
        return ""
    url = str(url).strip()
    if not url:
        return ""
    url = re.sub(r"^https?://[^/]+", "", url)
    return url.lstrip("/")

def platform_path(path):
    """Преобразование пути для текущей платформы"""
    return os.path.normpath(path)

def clean_title(title):
    """Очистка заголовка от служебных символов"""
    if not title:
        return "Неизвестно"

    title = str(title).strip()

    title = re.sub(r'^\d{1,3}\s*[-.]\s*', '', title)

    replacements = {
        '7c2': '/',
        '7c3': '"',
        '7c4': '?',
        '7c5': '*',
        '7c6': ':',
        '7c7': '\\',
        '7c8': '|',
        '7c9': '<',
        '7c10': '>',
    }
    for code, char in replacements.items():
        title = title.replace(code, char)

    return title if title else "Неизвестно"

def clean_artist_name(name):
    """Очистка имени исполнителя от кодов (используем те же правила, что и для заголовков)"""
    return clean_title(name)

def format_duration(ms):
    """Форматирование продолжительности в читаемый вид"""
    if ms is None or ms <= 0:
        return "0:00"

    seconds = ms // 1000
    minutes = seconds // 60
    seconds = seconds % 60
    return f"{minutes}:{seconds:02d}"

import unicodedata
import string

_PUNCT_TABLE = str.maketrans({c: " " for c in string.punctuation + "«»—–-‐-…“”’‚„―·•‒"})
_SPACE_RE = re.compile(r"\s+")

def normalize_cyr(s: str) -> str:
    """
    Нормализует русские строки для поиска:
    - переводит в lowercase
    - заменяет 'ё' -> 'е'
    - удаляет пунктуацию
    - схлопывает пробелы
    """
    if not s:
        return ""
    s = str(s).lower()
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("ё", "е")

    s = s.replace("7c2", "/").replace("7c3", '"').replace("7c4", "?").replace("7c5", "*").replace("7c6", ":") \
         .replace("7c7", "\\").replace("7c8", "|").replace("7c9", "<").replace("7c10", ">")
    s = s.translate(_PUNCT_TABLE)
    s = _SPACE_RE.sub(" ", s).strip()
    return s

def match_score(needle: str, hay: str) -> int:
    """
    Простейшее скоринг-правило:
    100 — точное совпадение,
     70 — совпадение слова целиком,
     50 — начинается с,
     20 — подстрока,
      0 — нет совпадения.
    """
    if not needle or not hay:
        return 0
    if needle == hay:
        return 100

    if f" {needle} " in f" {hay} ":
        return 70
    if hay.startswith(needle):
        return 50
    if needle in hay:
        return 20
    return 0

def token_score(needle: str, hay: str) -> int:
    """
    Мягкий токенный скоринг: считаем долю совпавших слов.
    Возвращаем 0..80 баллов, чтобы не перебивать точные совпадения (100).
    """
    if not needle or not hay:
        return 0
    qs = set(normalize_cyr(needle).split())
    hs = set(normalize_cyr(hay).split())
    if not qs or not hs:
        return 0
    inter = len(qs & hs)
    if inter == 0:
        return 0

    ratio = inter / len(qs)

    return int(round(min(1.0, ratio) * 80))
