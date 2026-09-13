"""Request-scoped UI translations. Production content is never translated here."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from jinja2 import pass_context

COOKIE = "studio_ui_language"
LANGUAGES = {"en": "English", "ru": "Русский"}
RU = json.loads((Path(__file__).parent / "locales" / "ru.json").read_text())


def language(request):
    value = request.cookies.get(COOKIE, "en")
    return value if value in LANGUAGES else "en"


def translate(value, lang="en", **values):
    original = str(value or "")
    key = re.sub(r"\s+", " ", original.strip())
    translated = RU.get(key, original) if lang == "ru" else original
    return translated.format(**values) if values else translated


@pass_context
def gettext(context, value, **values):
    return translate(value, language(context["request"]), **values)


@pass_context
def notice(context, value):
    """Translate known application diagnostics, retaining unknown provider detail."""
    text = str(value or "")
    if language(context["request"]) != "ru":
        return text
    if '\n' in text:
        return '\n'.join(notice(context, line) for line in text.split('\n'))
    if re.sub(r"\s+", " ", text.strip()) in RU:
        return translate(text, "ru")
    if ": " in text:
        category, detail = text.split(": ", 1)
        if detail in RU:
            return category + ": " + RU[detail]
    # Only known application wording is rewritten. This filter must not be used
    # for scripts, prompts, descriptions, user titles, or dialogue.
    replacements = [
        (r'^Configure before production: (.+)$', r'До запуска настройте: \1'),
        (r'^Assign ElevenLabs voices for (.+), or choose Native scene audio\.$', r'Назначьте голоса ElevenLabs персонажам: \1. Либо выберите встроенную речь.'),
        (r'^(FAL_\w+): this production adapter requires (.+)\. Other models need a separate integration\.$', r'\1: текущая интеграция рассчитана на \2. Другую модель нужно подключать отдельно.'),
        (r'^IMAGE_RESOLUTION: supported with current reference pricing: (.+)$', r'IMAGE_RESOLUTION: для референсов с текущим расчётом стоимости поддерживаются \1.'),
        (r'^(VIDEO_RESOLUTION|LIPSYNC_VARIANT|PROVIDER_INPUT_MODE): choose (.+)\.$', r'\1: выберите \2.'),
        (r'^(ffmpeg|ffprobe): required on the production server before starting\.$', r'\1: до запуска нужно установить на сервере производства.'),
        (r'^(PRICE_\w+): enter a finite, non-negative price\.$', r'\1: укажите корректную неотрицательную цену.'),
        (r'^(\w+): include the unfinished prerequisite stages: (.+)$', r'\1: также включите необходимые незавершённые этапы: \2.'),
        (r"episode (\S+) rejected:", r"Эпизод \1 не прошёл проверку:"),
        (r"total (\d+(?:\.\d+)?)s not within (\d+)[–-](\d+)", r"Длительность \1 сек. не входит в диапазон \2–\3 сек."),
        (r"cliffhanger\.hook is empty", "Не заполнена интрига в финале эпизода."),
        (r"Character '([^']+)' saved\.", r"Персонаж «\1» сохранён."),
        (r"Script v(\d+) saved\. (\d+) scenes rebuilt\.", r"Сценарий версии \1 сохранён. Обновлено сцен: \2."),
        (r"(.+): connected\.", r"\1: настроено."),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    return text


def safe_return(value, base):
    """Language selection can return only to a page inside this studio."""
    value = value or base + "/"
    decoded = value
    for _ in range(4):
        newer = unquote(decoded)
        if newer == decoded:
            break
        decoded = newer
    if any(ord(c) < 32 for c in decoded) or "\\" in decoded:
        return base + "/"
    try:
        parsed = urlsplit(decoded)
    except ValueError:
        return base + "/"
    if parsed.scheme or parsed.netloc or not decoded.startswith("/") or decoded.startswith("//"):
        return base + "/"
    if base and parsed.path != base and not parsed.path.startswith(base + "/"):
        return base + "/"
    if any(part == ".." for part in parsed.path.split("/")):
        return base + "/"
    return value


def context(request):
    from .config import settings
    path = request.url.path
    if not settings.base_path or not (path == settings.base_path or path.startswith(settings.base_path + "/")):
        path = settings.url(path)
    return {"ui_language": language(request), "ui_languages": LANGUAGES,
            "language_next": path}
