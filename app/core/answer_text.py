"""Remove citation annotations from user-facing chat text."""
import re

_LABEL = r"(?:sources?|references?|citations?|তথ্যসূত্র|সূত্র|উৎস|sutro|utsho)"
_MARKER = rf"[*_]*\s*{_LABEL}\s*[*_]*\s*[:：]\s*[*_]*"
_INLINE = re.compile(
    rf"[ \t]*(?:\(\s*{_MARKER}(?:[^()\r\n]|\([^()\r\n]*\))*\)"
    rf"|\[\s*{_MARKER}[^\]\r\n]*\])(?P<period>\.)?",
    re.IGNORECASE,
)
_REFERENCE_LINE = re.compile(
    rf"^\s*(?:#{{1,6}}\s+|[-*>]\s+)?(?:{_MARKER}|[*_]*{_LABEL}[*_]*\s*$)",
    re.IGNORECASE,
)
_DOCUMENT_LINE = re.compile(
    r"^\s*(?:[-*+]|\d+[.)])?\s*[^!\r\n]*\.(?:pdf|docx?|txt|md)[*_.)\x60\]]*\s*$",
    re.IGNORECASE,
)


def strip_source_references(text: str) -> str:
    def remove_inline(match: re.Match) -> str:
        preceding = text[match.start() - 1] if match.start() else ""
        return "." if match["period"] and preceding and preceding not in ".!?।\n" else ""

    clean = _INLINE.sub(remove_inline, text)
    lines = []
    in_references = False
    for line in clean.splitlines():
        if _REFERENCE_LINE.match(line):
            in_references = True
            continue
        if in_references and _DOCUMENT_LINE.fullmatch(line):
            continue
        if line.strip():
            in_references = False
        if lines or line.strip():
            lines.append(line)
    return "\n".join(lines).strip()
