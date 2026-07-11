"""Arabic-correct text normalization — ONE function for index AND query.

The FTS mirror stores a normalized shadow of every message and normalizes
every query string with the SAME function, so orthographic variants match:
a body containing "تمت الإحاطة" is found by searching "الاحاطه" (alef-hamza
variant + teh-marbuta/heh) and by the undiacritized form of any diacritized
word. Folds applied:

- strip tashkeel/diacritics  U+064B–U+0652 and the dagger alef U+0670
- strip tatweel              U+0640
- fold alef variants         أ / إ / آ / ٱ → ا
- fold alef maqsura          ى → ي
- fold teh marbuta           ة → ه
- fold hamza carriers        ؤ → و,  ئ → ي
- strip bidi/control marks   (reuses bodyclean._BIDI_RE)
- fold Arabic-Indic digits   ٠-٩ (U+0660–0669) and ۰-۹ (U+06F0–06F9) → 0-9

ASCII passes through untouched (FTS5's unicode61 tokenizer case-folds it),
so mixed Arabic/English queries work. The FTS5 tokenizer additionally runs
``unicode61 remove_diacritics 2`` as a second safety layer.
"""

import re

from .bodyclean import _BIDI_RE

# Tashkeel U+064B..U+0652 plus the superscript (dagger) alef U+0670.
_DIACRITICS_RE = re.compile("[ً-ْٰ]")

_FOLDS = str.maketrans({
    "ـ": None,   # tatweel
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",   # alef variants
    "ى": "ي",          # alef maqsura
    "ة": "ه",          # teh marbuta
    "ؤ": "و",          # hamza on waw
    "ئ": "ي",          # hamza on yeh
    # Arabic-Indic digits (U+0660-0669)
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    # Extended Arabic-Indic digits (U+06F0-06F9)
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
})


def normalize_ar(text: str) -> str:
    """Normalize Arabic orthography for indexing/search. Idempotent."""
    if not text:
        return ""
    text = _BIDI_RE.sub("", text)
    text = _DIACRITICS_RE.sub("", text)
    return text.translate(_FOLDS)


_FTS_TOKEN_RE = re.compile(r"[^\s\"'()*:^-]+")


def fts_match_expression(query: str) -> str:
    """Turn a user query into a safe FTS5 MATCH expression.

    Each whitespace token is normalized, stripped of FTS5 operator
    characters and double-quoted (implicit AND between tokens). A trailing
    ``*`` per token enables prefix matching so partial words still hit.
    Returns "" when nothing searchable remains.
    """
    tokens = _FTS_TOKEN_RE.findall(normalize_ar(query or ""))
    return " ".join(f'"{t}"*' for t in tokens if t)
