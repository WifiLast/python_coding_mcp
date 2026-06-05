from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from typing import Any


# ---------------------------------------------------------------------------
# Trie (exact multi-string matching)
# ---------------------------------------------------------------------------

@dataclass
class _TrieNode:
    children: dict[str, "_TrieNode"] = field(default_factory=dict)
    terminal: bool = False


def _insert(root: _TrieNode, text: str) -> None:
    node = root
    for char in text:
        node = node.children.setdefault(char, _TrieNode())
    node.terminal = True


def _is_single_literal(fragment: str) -> bool:
    return len(fragment) == 1 and fragment not in {"", "|", "(", ")", "[", "]", "{", "}", "^", "$"}


def _escape_class_char(char: str) -> str:
    if char in {"\\", "]", "-", "^"}:
        return "\\" + char
    return char


def _maybe_char_class(parts: list[tuple[str, str]]) -> str | None:
    literals: list[str] = []
    for char, suffix in parts:
        if suffix:
            return None
        if len(char) != 1:
            return None
        literals.append(char)
    if len(literals) < 2:
        return None
    return "[" + "".join(_escape_class_char(char) for char in literals) + "]"


def _render(node: _TrieNode) -> str:
    child_parts = []
    for char in sorted(node.children):
        child = node.children[char]
        child_suffix = _render(child)
        child_parts.append((char, child_suffix))

    if not child_parts:
        return ""

    class_fragment = _maybe_char_class(child_parts)
    if class_fragment is not None:
        if node.terminal:
            return f"(?:{class_fragment})?"
        return class_fragment

    options = [re.escape(char) + suffix for char, suffix in child_parts]
    if node.terminal:
        if len(options) == 1:
            return f"(?:{options[0]})?"
        return f"(?:{'|'.join(['', *options])})"
    if len(options) == 1:
        return options[0]
    return f"(?:{'|'.join(options)})"


# ---------------------------------------------------------------------------
# Token/segment classifiers used by generalization strategies
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9]+$")
_ALNUM_EXT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _generalize_segment(segment: str) -> str:
    """Generalize a single path/URL segment."""
    if segment == "":
        return ""
    if segment.isdigit():
        return r"\d+"
    if _ALNUM_EXT_RE.fullmatch(segment):
        return r"[A-Za-z0-9._-]+"
    return re.escape(segment)


def _generalize_token(tokens: list[str]) -> str:
    """Return a regex fragment for a column of tokens across multiple strings.

    Constant columns → re.escape(literal).
    Variable columns → narrowest matching character class.
    """
    unique = set(tokens)
    if len(unique) == 1:
        return re.escape(next(iter(unique)))
    if all(_UUID_RE.fullmatch(t) for t in unique):
        return r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    if all(_DATETIME_RE.match(t) for t in unique):
        return r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    if all(_DATE_RE.fullmatch(t) for t in unique):
        return r"\d{4}-\d{2}-\d{2}"
    if all(t.isdigit() for t in unique):
        return r"\d+"
    if all(_HEX_RE.fullmatch(t) for t in unique):
        return r"[0-9a-fA-F]+"
    if all(_SLUG_RE.fullmatch(t) for t in unique):
        return r"[A-Za-z0-9]+"
    if all(_ALNUM_EXT_RE.fullmatch(t) for t in unique):
        return r"[A-Za-z0-9._-]+"
    return r".+"


# ---------------------------------------------------------------------------
# Generalization strategies — each returns (pattern, strategy_name) or None
# ---------------------------------------------------------------------------

def _try_url_generalization(texts: list[str], anchored: bool) -> tuple[str, str] | None:
    """Generalize one or more URLs that share the same host."""
    parsed = [urlsplit(t) for t in texts]
    if not all(p.scheme and p.netloc for p in parsed):
        return None

    hosts = {p.netloc for p in parsed}
    if len(hosts) != 1:
        return None

    schemes = {p.scheme for p in parsed}
    if schemes <= {"http", "https"}:
        scheme_pat = "https?"
    elif len(schemes) == 1:
        scheme_pat = re.escape(next(iter(schemes)))
    else:
        scheme_pat = f"(?:{'|'.join(re.escape(s) for s in sorted(schemes))})"

    host_pat = re.escape(next(iter(hosts)))

    all_segs = [[s for s in p.path.split("/") if s] for p in parsed]
    seg_counts = {len(s) for s in all_segs}

    if len(seg_counts) != 1:
        path_pat = r"/.*"
    else:
        path_parts: list[str] = []
        multi = len(texts) > 1
        for col in zip(*all_segs):
            unique = set(col)
            if len(unique) == 1:
                # Single URL: generalize each segment on its own merit.
                # Multiple URLs: constant segment — keep literal.
                seg = next(iter(unique))
                path_parts.append(_generalize_segment(seg) if not multi else re.escape(seg))
            else:
                path_parts.append(_generalize_token(list(col)))
        path_pat = "".join(f"/{p}" for p in path_parts)
        if parsed[0].path.endswith("/") and path_pat:
            path_pat += "/"

    pattern = f"{scheme_pat}://{host_pat}{path_pat}"
    if anchored:
        pattern = f"^{pattern}$"
    return pattern, "url_generalization"


def _try_digit_substitution(texts: list[str], anchored: bool) -> tuple[str, str] | None:
    """Replace all digit runs with \\d+ when every string shares the same template."""
    _SENTINEL = "\x00"
    templates = [re.sub(r"\d+", _SENTINEL, t) for t in texts]
    if len(set(templates)) != 1:
        return None
    if _SENTINEL not in templates[0]:
        return None  # no digits — nothing to generalize

    pattern = re.escape(templates[0]).replace(re.escape(_SENTINEL), r"\d+")
    if anchored:
        pattern = f"^{pattern}$"
    return pattern, "digit_substitution"


def _common_prefix(texts: list[str]) -> str:
    return os.path.commonprefix(texts)


def _common_suffix(texts: list[str]) -> str:
    rev = _common_prefix([t[::-1] for t in texts])
    return rev[::-1]


def _try_prefix_suffix(texts: list[str], anchored: bool) -> tuple[str, str] | None:
    """Generalize strings that share a common prefix and/or suffix."""
    if len(texts) < 2:
        return None

    prefix = _common_prefix(texts)
    suffix = _common_suffix(texts)

    # Guard against overlap when prefix + suffix exceed the shortest string
    min_len = min(len(t) for t in texts)
    overlap = len(prefix) + len(suffix) - min_len
    if overlap > 0:
        suffix = suffix[overlap:]

    middles = [
        t[len(prefix) : len(t) - len(suffix)] if suffix else t[len(prefix):]
        for t in texts
    ]
    mid_pat = _generalize_token(middles)

    # Skip when there is no anchoring context and the middle cannot be narrowed
    if not prefix and not suffix and mid_pat == r".+":
        return None

    pattern = re.escape(prefix) + mid_pat + re.escape(suffix)
    if anchored:
        pattern = f"^{pattern}$"
    return pattern, "prefix_suffix"


def _try_segmented(texts: list[str], anchored: bool) -> tuple[str, str] | None:
    """Split by a common delimiter and generalize each column independently."""
    for delim in ("/", "-", "_", "."):
        splits = [t.split(delim) for t in texts]
        lengths = {len(s) for s in splits}
        if len(lengths) != 1 or next(iter(lengths)) < 2:
            continue

        parts: list[str] = []
        any_generalized = False
        for col in zip(*splits):
            tok = _generalize_token(list(col))
            parts.append(tok)
            if len(set(col)) > 1:
                any_generalized = True

        if not any_generalized:
            continue

        escaped_delim = re.escape(delim)
        pattern = escaped_delim.join(parts)
        if anchored:
            pattern = f"^{pattern}$"
        return pattern, f"segmented_{delim}"

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_regex_rule(
    strings: list[str],
    anchored: bool = True,
    unique: bool = True,
    generalize: bool = False,
) -> dict[str, Any]:
    """Generate a regex rule that matches the supplied strings.

    When *generalize* is True the function tries the following strategies in
    order and returns the first that produces a meaningful pattern:

    1. url_generalization  — all inputs are URLs sharing the same host
    2. digit_substitution  — inputs share a template, differ only in digit runs
    3. prefix_suffix       — inputs share a common prefix and/or suffix
    4. segmented_<delim>   — inputs split by /, -, _, or . with constant/variable columns

    Falls back to an exact-match trie when no strategy applies.
    """
    cleaned = [str(item) for item in strings if str(item) != "" or item == ""]
    if unique:
        cleaned = list(dict.fromkeys(cleaned))

    if not cleaned:
        return {
            "ok": False,
            "reason": "empty_input",
            "pattern": "",
            "compiled": None,
            "strings": [],
            "count": 0,
            "strategy": "none",
        }

    pattern: str | None = None
    strategy = "trie"

    if generalize:
        result = (
            _try_url_generalization(cleaned, anchored)
            or _try_digit_substitution(cleaned, anchored)
            or _try_prefix_suffix(cleaned, anchored)
            or _try_segmented(cleaned, anchored)
        )
        if result is not None:
            pattern, strategy = result

    if pattern is None:
        root = _TrieNode()
        for text in cleaned:
            _insert(root, text)
        body = _render(root)
        pattern = f"^{body}$" if anchored else body
        strategy = "trie"

    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return {
            "ok": False,
            "reason": "compile_error",
            "pattern": pattern,
            "compiled": None,
            "error": str(exc),
            "strings": cleaned,
            "count": len(cleaned),
            "strategy": strategy,
        }

    return {
        "ok": True,
        "pattern": pattern,
        "compiled": compiled.pattern,
        "strings": cleaned,
        "count": len(cleaned),
        "strategy": strategy,
    }
