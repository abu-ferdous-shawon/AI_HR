"""Persist whole generations of four FAISS indexes, isolated by site."""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import faiss
import numpy as np
from filelock import FileLock

from app.indexer.cards import TIERS
from app.indexer.embeddings import EmbeddingError
from app.indexer.util import json_text


class IndexError(RuntimeError):
    """A metadata snapshot is absent, incompatible or invalid."""


def write_json(path: Path, payload) -> None:
    path.write_text(json_text(payload) + "\n", encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class IndexStore:
    def __init__(self, root: str, site: str):
        self.path = Path(root).expanduser().resolve() / site

    def lock(self, purpose: str) -> FileLock:
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return FileLock(self.path / f".{purpose}.lock")

    def active(self) -> Path | None:
        pointer = self.path / "CURRENT"
        if not pointer.exists():
            return None
        generation = pointer.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"snapshot-[a-z0-9_]+", generation):
            raise IndexError("Invalid metadata snapshot pointer")
        target = self.path / generation
        if not target.is_dir():
            raise IndexError("The current metadata snapshot directory is missing")
        return target

    def publish(self, workspace: Path) -> None:
        """Publish under the snapshot lock and retire the previous generation."""
        with self.lock("snapshot"):
            previous = self.active()
            pointer = self.path / ".CURRENT.next"
            pointer.write_text(workspace.name + "\n", encoding="ascii")
            os.replace(pointer, self.path / "CURRENT")
            if previous is not None:
                shutil.rmtree(previous, ignore_errors=True)

    def workspace(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="snapshot-", dir=self.path))


def read_tier(path: Path, tier: str) -> tuple[list[dict], faiss.Index]:
    if tier not in TIERS:
        raise ValueError(f"Unknown index tier {tier}")
    cards = read_json(path / f"{tier}.json")
    index = faiss.read_index(str(path / f"{tier}.faiss"))
    if len(cards) != index.ntotal:
        raise IndexError(f"{tier} card count does not match the FAISS index")
    return cards, index


def _vectors(embedder, texts: list[str], *, query: bool = False) -> np.ndarray:
    array = np.asarray(list(embedder.encode(texts, query=query)), dtype=np.float32)
    if array.shape != (len(texts), embedder.dimension) or not np.isfinite(array).all():
        raise EmbeddingError("The embedding provider returned invalid vectors")
    if np.any(np.linalg.norm(array, axis=1) == 0):
        raise EmbeddingError("The embedding provider returned an empty vector")
    faiss.normalize_L2(array)
    return array


def write_tier(path: Path, tier: str, cards: list[dict], embedder, previous: Path | None) -> int:
    reusable = {}
    if previous is not None:
        old_cards, old_index = read_tier(previous, tier)
        if old_index.d == embedder.dimension:
            reusable = {card["key"]: (card["text"], i) for i, card in enumerate(old_cards)}
    index = faiss.IndexFlatIP(embedder.dimension)
    embedded = 0
    for start in range(0, len(cards), 256):
        batch = cards[start:start + 256]
        matrix = np.empty((len(batch), embedder.dimension), dtype=np.float32)
        pending = []
        for position, card in enumerate(batch):
            hit = reusable.get(card["key"])
            if hit is not None and hit[0] == card["text"]:
                matrix[position] = old_index.reconstruct(hit[1])
            else:
                pending.append(position)
        if pending:
            matrix[pending] = _vectors(embedder, [batch[i]["text"] for i in pending])
            embedded += len(pending)
        index.add(matrix)
    write_json(path / f"{tier}.json", cards)
    faiss.write_index(index, str(path / f"{tier}.faiss"))
    return embedded


def search_tier(cards: list[dict], index, embedder, query: str, limit: int,
                doctype: str | None = None, *, preferred_keys=()) -> list[dict]:
    if not query.strip() or limit < 1:
        raise ValueError("Search requires a non-empty query and positive limit")
    if index.d != embedder.dimension:
        raise IndexError("The embedding dimension changed. Sync metadata before searching.")
    if not cards:
        return []
    vector = _vectors(embedder, [query], query=True)
    result = []
    added = set()
    if preferred_keys:
        positions_by_key = {card["key"]: i for i, card in enumerate(cards)}
        for key in preferred_keys:
            position = positions_by_key.get(key)
            if position is None or position in added:
                continue
            card = cards[position]
            if doctype and card["data"].get("doctype") != doctype:
                continue
            score = float(vector[0] @ index.reconstruct(position))
            result.append({"key": key, "score": score, "data": card["data"]})
            added.add(position)
            if len(result) == limit:
                return result
    size = index.ntotal if doctype else min(limit, index.ntotal)
    scores, positions = index.search(vector, size)
    for score, position in zip(scores[0], positions[0]):
        if position in added:
            continue
        card = cards[position]
        if doctype and card["data"].get("doctype") != doctype:
            continue
        result.append({"key": card["key"], "score": float(score), "data": card["data"]})
        if len(result) == limit:
            break
    return result
