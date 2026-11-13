import json
from pathlib import Path

_TRANSLATIONS = {}
_CURRENT_LANG = "en"

def init_i18n(default_lang: str = "en") -> None:
    global _CURRENT_LANG, _TRANSLATIONS
    _CURRENT_LANG = default_lang
    locales_dir = Path(__file__).parent / "locales"
    for lang_file in locales_dir.glob("*.json"):
        lang_code = lang_file.stem
        try:
            _TRANSLATIONS[lang_code] = json.loads(lang_file.read_text(encoding="utf-8"))
        except Exception:
            pass

def set_language(lang: str) -> None:
    global _CURRENT_LANG
    if lang in _TRANSLATIONS:
        _CURRENT_LANG = lang

def tr(key: str) -> str:
    lang_table = _TRANSLATIONS.get(_CURRENT_LANG, _TRANSLATIONS.get("en", {}))
    if key in lang_table:
        return lang_table[key]
    # Fallback naar Engels
    return _TRANSLATIONS.get("en", {}).get(key, key)
