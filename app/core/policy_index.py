import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("hr_ai.policy")

SUPPORTED = {".txt", ".md", ".pdf", ".docx"}
K1, B = 1.5, 0.75  # standard BM25 constants

# Split on spaces and punctuation, including the Bangla danda. Letters and
# Bangla vowel signs stay together, so Bangla words are not cut apart.
SPLIT_RE = re.compile(r"[\s.,;:!?()\[\]{}\"'“”‘’।/\\|\-–—*#_=<>]+")
STOPWORDS = {"the", "a", "an", "of", "is", "are", "to", "and", "in", "for", "on",
             "at", "by", "or", "be", "how", "what", "do", "does", "my", "i", "can",
             "me", "it", "this", "that", "with"}


def stem(word: str) -> str:
    """A very small plural fix: holidays -> holiday, days -> day."""
    if len(word) > 3 and word.isascii() and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    return [stem(t) for t in SPLIT_RE.split(text.lower()) if t and t not in STOPWORDS]


# Words that can mean the same thing in HR documents. Edit freely: add the
# words your documents use (Bangla words work too).
SYNONYM_GROUPS = [
    {"annual", "earned", "privilege"},
    {"sick", "illness", "medical"},
    {"maternity", "pregnancy"},
    {"salary", "pay", "remuneration", "wage"},
    {"resign", "resignation"},
    {"holiday", "festival"},
    {"leave", "absence"},
    {"bonus", "incentive"},
    {"probation", "probationary"},
]
SYNONYMS: dict[str, set[str]] = {}
for _group in SYNONYM_GROUPS:
    _stemmed = {stem(w) for w in _group}
    for _word in _stemmed:
        SYNONYMS[_word] = _stemmed - {_word}


def _strip_repeated_lines(pages: list[str]) -> list[str]:
    """Remove page headers and footers: short lines that repeat on many pages."""
    if len(pages) < 4:
        return pages

    def key(line: str) -> str:
        return re.sub(r"\d+", "#", line.strip().lower())   # page numbers differ

    seen_on = Counter(key(line) for page in pages
                      for line in set(page.splitlines())
                      if line.strip() and len(line) < 120)
    limit = max(3, int(0.4 * len(pages)))
    repeated = {k for k, n in seen_on.items() if n >= limit}
    return ["\n".join(line for line in page.splitlines() if key(line) not in repeated)
            for page in pages]


def read_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        from pypdf import PdfReader
        pages = [(p.extract_text() or "") for p in PdfReader(str(path)).pages]
        return "\n\n".join(_strip_repeated_lines(pages))
    if suffix == ".docx":
        import docx
        document = docx.Document(str(path))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            parts += [" | ".join(c.text for c in row.cells) for row in table.rows]
        return "\n\n".join(parts)
    return ""


def chunk_text(text: str, max_chars: int = 900) -> list[str]:
    """Group paragraphs into pieces of roughly max_chars."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) + 2 > max_chars:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)

    pieces: list[str] = []
    for chunk in chunks:           # a very long paragraph is cut into pieces
        while len(chunk) > 2 * max_chars:
            pieces.append(chunk[:max_chars])
            chunk = chunk[max_chars:]
        pieces.append(chunk)
    return pieces


@dataclass
class Chunk:
    source: str
    text: str
    tf: Counter = field(default_factory=Counter)
    length: int = 0


class PolicyIndex:
    """Keyword search (BM25) over the files in one folder. Rebuilds itself when files change."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._signature = None
        self._chunks: list[Chunk] = []
        self._df: Counter = Counter()
        self._avg_len = 1.0

    def _files(self) -> list[Path]:
        if not self.folder.is_dir():
            return []
        return sorted(p for p in self.folder.rglob("*")
                      if p.is_file() and p.suffix.lower() in SUPPORTED
                      and not p.name.startswith((".", "~$")))

    def refresh(self) -> None:
        files = self._files()
        signature = tuple((str(p), p.stat().st_mtime_ns) for p in files)
        if signature == self._signature:
            return
        chunks: list[Chunk] = []
        for path in files:
            try:
                text = read_text(path)
            except Exception as exc:
                log.warning("Could not read %s: %s", path.name, exc)
                continue
            if not text.strip():
                log.warning("No text found in %s (a scanned PDF?)", path.name)
                continue
            for piece in chunk_text(text):
                tokens = tokenize(piece)
                if tokens:
                    chunks.append(Chunk(path.name, piece, Counter(tokens), len(tokens)))
        self._chunks = chunks
        self._df = Counter(term for c in chunks for term in c.tf)
        self._avg_len = (sum(c.length for c in chunks) / len(chunks)) if chunks else 1.0
        self._signature = signature

    def search(self, query: str, k: int = 6) -> list[dict]:
        self.refresh()
        terms = set(tokenize(query))
        for term in list(terms):                 # add words that mean the same
            terms |= SYNONYMS.get(term, set())
        total = len(self._chunks)
        if not total or not terms:
            return []
        scored = []
        for chunk in self._chunks:
            score = 0.0
            for term in terms:
                tf = chunk.tf.get(term, 0)
                if not tf:
                    continue
                df = self._df[term]
                idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
                score += idf * tf * (K1 + 1) / (
                    tf + K1 * (1 - B + B * chunk.length / self._avg_len))
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [{"source": c.source, "text": c.text} for _, c in scored[:k]]