"""Deterministic, dependency-free email body cleaning (stdlib only).

Email bodies returned to an LLM are bloated by quoted reply chains,
signatures and disclaimers. This module strips them without any third
party dependency and without importing exchangelib. Bilingual: handles
both English and Arabic (Outlook / Gmail) reply conventions, and is
tolerant of RTL/LTR bidi control marks and ``>`` quote prefixes.

Public API:
    strip_quoted_history(text) -> (latest_reply_text, markers_stripped)
    strip_signature(text)      -> text without a trailing signature
    clean_body(text, max_chars) -> {"text", "quoted_blocks_stripped",
                                    "truncated", "original_chars"}
    html_to_text(html)         -> minimal readable plain text
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

# --------------------------------------------------------------------------
# Line normalization helpers
# --------------------------------------------------------------------------

# Bidi / directionality control characters commonly injected by Outlook and
# Gmail around Arabic text (LRM, RLM, embeddings, isolates, ALM, BOM).
_BIDI_RE = re.compile(
    "[\\u200e\\u200f\\u202a-\\u202e\\u2066-\\u2069\\u061c\\ufeff]"
)

# One or more leading '>' quote markers ("> ", ">> ", "> > " ...).
_QUOTE_PREFIX_RE = re.compile(r"^(?:>\s?)+")

# Normalize Arabic alef hamza variants so marker matching tolerates both
# spellings (e.g. "الإرسال" vs "الارسال", "أرسلت" vs "ارسلت").
_AR_TRANS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا"})


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _norm_line(line: str) -> tuple[str, bool]:
    """Return (normalized_line, was_quoted).

    Normalization: drop bidi control marks, strip surrounding whitespace,
    and remove leading '>' quote prefixes (recording that they were there).
    """
    s = _BIDI_RE.sub("", line).strip()
    m = _QUOTE_PREFIX_RE.match(s)
    quoted = m is not None
    if m:
        s = s[m.end():].strip()
    return s, quoted


# --------------------------------------------------------------------------
# Reply-history markers
# --------------------------------------------------------------------------

# "-----Original Message-----" separator (Outlook EN).
_ORIG_EN_RE = re.compile(r"^-{2,}\s*original message\s*-{2,}$", re.IGNORECASE)
# Arabic "رسالة أصلية" variants ("الرسالة الأصلية", dash-decorated, hamza
# normalized to bare alef before matching).
_ORIG_AR_RE = re.compile(r"^[-_*\s]*(?:ال)?رسالة\s+ال?اصلية[-_*\s:]*$")

# Outlook EN header block: "From:" paired with a following "Sent:"/"Date:".
_FROM_EN_RE = re.compile(r"^from\s*:", re.IGNORECASE)
_PAIR_EN_RE = re.compile(r"^(?:sent|date)\s*:", re.IGNORECASE)

# Outlook AR header block: "من:" paired with "تاريخ الإرسال:"/"التاريخ:"/
# "أرسلت:" (hamza-normalized).
_FROM_AR_RE = re.compile(r"^من\s*:")
_PAIR_AR_RE = re.compile(r"^(?:تاريخ\s+الارسال|التاريخ|ارسلت)\s*:")

# Gmail-style EN attribution: "On Mon, Jun 1, 2026 ... <a@b> wrote:".
_GMAIL_EN_RE = re.compile(r"^On .{4,80} wrote:\s*$", re.IGNORECASE)

# Gmail-style AR attribution heuristic ingredients (kept conservative).
_AR_WROTE_RE = re.compile(r"\bكتبت?\b")
_EMAIL_FRAG_RE = re.compile(r"<[^<>\s]+@[^<>\s]+>")
_DATEISH_RE = re.compile(r"\d{4}|\d{1,2}[/:\-]\d{1,2}")

# Minimum length of a run of '>'-prefixed lines treated as a quoted block.
_MIN_QUOTE_RUN = 3


def _find_markers(
    lines: list[str],
) -> tuple[list[tuple[int, str]], list[tuple[int, int]]]:
    """Return (markers, quote_runs).

    markers: sorted list of (line_index, kind) where kind is one of
        "original", "outlook_en", "outlook_ar", "gmail_en", "gmail_ar",
        "quote_run".
    quote_runs: list of (start_index, end_index) inclusive spans of runs of
        3+ consecutive '>'-quoted lines.
    """
    n = len(lines)
    norm = [_norm_line(ln) for ln in lines]
    markers: list[tuple[int, str]] = []
    runs: list[tuple[int, int]] = []

    # Runs of 3+ consecutive '>'-quoted lines.
    i = 0
    while i < n:
        if norm[i][1]:
            j = i
            while j < n and norm[j][1]:
                j += 1
            if j - i >= _MIN_QUOTE_RUN:
                runs.append((i, j - 1))
                markers.append((i, "quote_run"))
            i = j
        else:
            i += 1

    for idx in range(n):
        s = norm[idx][0]
        if not s:
            continue
        ar = s.translate(_AR_TRANS)
        if _ORIG_EN_RE.match(s) or _ORIG_AR_RE.match(ar):
            markers.append((idx, "original"))
            continue
        if _FROM_EN_RE.match(s) and any(
            _PAIR_EN_RE.match(norm[k][0]) for k in range(idx + 1, min(idx + 5, n))
        ):
            markers.append((idx, "outlook_en"))
            continue
        if _FROM_AR_RE.match(ar) and any(
            _PAIR_AR_RE.match(norm[k][0].translate(_AR_TRANS))
            for k in range(idx + 1, min(idx + 5, n))
        ):
            markers.append((idx, "outlook_ar"))
            continue
        if _GMAIL_EN_RE.match(s):
            markers.append((idx, "gmail_en"))
            continue
        # Conservative Arabic "wrote:" attribution: must contain the word
        # كتب/كتبت, end with ':' and carry a date-ish or <email> fragment.
        if (
            len(s) <= 200
            and s.endswith(":")
            and _AR_WROTE_RE.search(s)
            and (_EMAIL_FRAG_RE.search(s) or _DATEISH_RE.search(s))
        ):
            markers.append((idx, "gmail_ar"))

    markers.sort(key=lambda m: m[0])
    return markers, runs


def strip_quoted_history(text: str) -> tuple[str, int]:
    """Cut the body at the first reply-history marker.

    Returns (latest-reply-only text, count of quoted blocks/markers found in
    the stripped tail). A marker sitting on the *first* non-empty line is
    ignored (a body that genuinely starts with a marker-like line survives)
    — except an "Original Message" separator, which may cut even that
    early. The second non-empty line is NOT protected: a one-line reply
    directly followed by the quoted chain is the most common email shape.
    Trailing whitespace lines are stripped from the result.
    """
    if not text:
        return ("", 0)
    t = _normalize_newlines(text)
    lines = t.split("\n")
    markers, runs = _find_markers(lines)

    nonempty = [i for i, ln in enumerate(lines) if ln.strip()]
    protected = set(nonempty[:1])

    cut: int | None = None
    for idx, kind in markers:
        if idx in protected and kind != "original":
            continue
        cut = idx
        break
    if cut is None:
        return (t.rstrip(), 0)

    # Count markers in the removed tail; markers that sit inside a counted
    # '>'-quoted run are absorbed by the run (counted once).
    run_spans = [(a, b) for (a, b) in runs if a >= cut]
    count = 0
    for idx, kind in markers:
        if idx < cut:
            continue
        if kind != "quote_run" and any(a <= idx <= b for (a, b) in run_spans):
            continue
        count += 1
    return ("\n".join(lines[:cut]).rstrip(), max(count, 1))


# --------------------------------------------------------------------------
# Signature stripping
# --------------------------------------------------------------------------

# (prefix, max length allowed AFTER the prefix on the same line).
# The remainder cap keeps lines like "Thanks, that works for me." safe.
_CLOSERS_EN: list[tuple[str, int]] = [
    ("best regards", 4),
    ("kind regards", 4),
    ("regards", 2),
    ("thanks,", 2),
    ("sent from my", 40),
]
# Matched against hamza-normalized text.
_CLOSERS_AR: list[tuple[str, int]] = [
    ("مع خالص التحية", 30),
    ("مع التحية", 30),
    ("تحياتي", 30),
    ("وتفضلوا بقبول", 60),
    ("ارسل من", 40),
]

_MAX_SIG_LINES = 6
_MAX_SIG_LINE_LEN = 80


def _is_closer_line(s: str) -> bool:
    cf = s.casefold()
    for prefix, max_rest in _CLOSERS_EN:
        if cf.startswith(prefix) and len(cf) - len(prefix) <= max_rest:
            return True
    ar = s.translate(_AR_TRANS)
    for prefix, max_rest in _CLOSERS_AR:
        if ar.startswith(prefix) and len(ar) - len(prefix) <= max_rest:
            return True
    return False


def strip_signature(text: str) -> str:
    """Remove a trailing signature block when one is confidently detected.

    Two detectors: the RFC "-- " delimiter line, and a trailing block of at
    most 6 short lines that begins with a common EN/AR closer. Conservative:
    only strips when at least 2 non-empty body lines remain above.
    """
    if not text:
        return ""
    t = _normalize_newlines(text).rstrip()
    if not t:
        return ""
    lines = t.split("\n")

    # RFC signature delimiter: a line that is exactly "--" / "-- ".
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].rstrip() == "--":
            above = sum(1 for ln in lines[:i] if ln.strip())
            below = sum(1 for ln in lines[i + 1:] if ln.strip())
            if above >= 2 and below <= 10:
                return "\n".join(lines[:i]).rstrip()
            break

    nonempty_idx = [i for i, ln in enumerate(lines) if ln.strip()]
    for i in nonempty_idx[-_MAX_SIG_LINES:]:
        s, _quoted = _norm_line(lines[i])
        if not _is_closer_line(s):
            continue
        block_ne = [ln for ln in lines[i:] if ln.strip()]
        if len(block_ne) > _MAX_SIG_LINES:
            continue
        if any(len(ln.strip()) > _MAX_SIG_LINE_LEN for ln in block_ne):
            continue
        if sum(1 for ln in lines[:i] if ln.strip()) < 2:
            continue
        return "\n".join(lines[:i]).rstrip()
    return t


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

_TRUNC_SUFFIX = "… [truncated]"
# 3+ consecutive blank lines (possibly whitespace-only) -> a single blank.
_BLANK_RUN_RE = re.compile(r"\n(?:[ \t]*\n){3,}")


def clean_body(text: str, max_chars: int = 4000) -> dict:
    """Full cleaning pipeline for an email body.

    normalize newlines -> strip quoted history -> strip signature ->
    collapse 3+ blank lines to 1 -> rstrip -> truncate at a word boundary.
    """
    raw = text or ""
    original_chars = len(raw)
    t = _normalize_newlines(raw)
    t, quoted_blocks = strip_quoted_history(t)
    t = strip_signature(t)
    t = _BLANK_RUN_RE.sub("\n\n", t)
    t = t.rstrip()

    truncated = False
    if max_chars and len(t) > max_chars:
        truncated = True
        budget = max(1, max_chars - len(_TRUNC_SUFFIX))
        cut = t[:budget]
        if budget < len(t) and not t[budget].isspace():
            # Mid-word: back up to the previous whitespace boundary.
            ws = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
            if ws > 0:
                cut = cut[:ws]
        t = cut.rstrip() + _TRUNC_SUFFIX

    return {
        "text": t,
        "quoted_blocks_stripped": quoted_blocks,
        "truncated": truncated,
        "original_chars": original_chars,
    }


# --------------------------------------------------------------------------
# Minimal HTML -> text (for reading)
# --------------------------------------------------------------------------

_BLOCK_TAGS = {
    "p", "div", "tr", "li", "table",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
_SKIP_TAGS = ("style", "script", "head")
_MAX_HREF_SHOWN = 80


def _norm_url(u: str) -> str:
    u = u.strip().casefold()
    u = re.sub(r"^(?:https?:)?//", "", u)
    u = re.sub(r"^mailto:", "", u)
    if u.startswith("www."):
        u = u[4:]
    return u.rstrip("/")


def _render_link(href: str, label: str) -> str:
    if not label:
        return href if href and len(href) <= _MAX_HREF_SHOWN else ""
    if not href or len(href) > _MAX_HREF_SHOWN or _norm_url(href) == _norm_url(label):
        return label
    return f"{label} ({href})"


class _HTMLToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip = 0
        self._href: str = ""
        self._label: list[str] | None = None

    def _append(self, s: str) -> None:
        if self._label is not None:
            self._label.append(" " if s == "\n" else s)
        else:
            self._out.append(s)

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "br":
            self._append("\n")
            return
        if tag in _BLOCK_TAGS:
            self._append("\n")
        if tag == "a":
            self._flush_link()
            self._href = (dict(attrs).get("href") or "").strip()
            self._label = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag == "a":
            self._flush_link()
            return
        if tag in ("td", "th"):
            self._append(" ")
        if tag in _BLOCK_TAGS:
            self._append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self._append(data)

    def _flush_link(self) -> None:
        if self._label is None:
            return
        label = " ".join("".join(self._label).split())
        href, self._href, self._label = self._href, "", None
        self._out.append(_render_link(href, label))

    def finish(self) -> str:
        self._flush_link()
        return "".join(self._out)


def html_to_text(html: str) -> str:
    """Minimal stdlib HTML -> text conversion for reading.

    Block tags (p, div, br, tr, li, h1-h6, table) emit newlines; style /
    script / head contents are dropped; entities are unescaped; links keep
    their href only when it is short and differs from the label. Whitespace
    is collapsed per line; Arabic text passes through untouched.
    """
    if not html:
        return ""
    parser = _HTMLToText()
    try:
        parser.feed(html)
        parser.close()
        raw = parser.finish()
    except Exception:
        # HTMLParser is lenient, but never let junk markup crash a read
        # path: fall back to a crude tag strip.
        raw = unescape(re.sub(r"<[^>]*>", " ", html))

    lines = [" ".join(ln.split()) for ln in raw.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln:
            out.append(ln)
        elif out and out[-1] != "":
            out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)
