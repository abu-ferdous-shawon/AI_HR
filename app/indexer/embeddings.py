"""Embedding backends for metadata cards and search queries."""
import hashlib
import re
from collections.abc import Iterator

import httpx2
import numpy as np

from app.core.sites import SiteConfig


class EmbeddingError(RuntimeError):
    pass


class LocalEmbeddings:
    """Deterministic word and character features for offline lexical search."""

    dimension = 512

    def encode(self, texts: list[str], *, query: bool = False) -> Iterator[np.ndarray]:
        for text in texts:
            vector = np.zeros(self.dimension, dtype=np.float32)
            for token in re.findall(r"[^\W_]+", text.casefold()):
                padded = f"^{token}$"
                grams = (padded[i:i + 3] for i in range(len(padded) - 2))
                for feature in (token, token, *grams):
                    slot = int.from_bytes(hashlib.blake2s(feature.encode(), digest_size=4).digest())
                    vector[slot % self.dimension] += 1 if slot & 512 else -1
            yield vector


class GeminiEmbeddings:
    dimension = 768

    def __init__(self, api_key: str, model: str):
        from google import genai, types

        if not api_key:
            raise EmbeddingError("Configure GEMINI_API_KEY or the site's gemini_api_key_env")
        self.client = genai.Client(
            api_key=api_key,
            vertexai=False,
            http_options=types.HttpOptions(
                timeout=30_000,
                retry_options=types.HttpRetryOptions(
                    attempts=3, max_delay=15, http_status_codes=[429, 500, 502, 503, 504],
                ),
            ),
        )
        self.model = model

    def encode(self, texts: list[str], *, query: bool = False) -> Iterator[np.ndarray]:
        from google.genai import errors, types

        model = self.model.removeprefix("models/")
        config = {"output_dimensionality": self.dimension}
        if model == "gemini-embedding-001":
            config["task_type"] = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
        batch_size = 32
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            try:
                response = self.client.models.embed_content(
                    model=self.model,
                    contents=[types.Content(parts=[types.Part.from_text(text=text)]) for text in batch],
                    config=types.EmbedContentConfig(**config),
                )
            except errors.APIError as exc:
                raise EmbeddingError(f"Gemini embedding request failed (HTTP {exc.code})") from None
            except httpx2.TransportError:
                raise EmbeddingError("Could not reach the Gemini embedding API") from None
            rows = response.embeddings or []
            if len(rows) != len(batch):
                raise EmbeddingError(f"{self.model} returned {len(rows)} embeddings for {len(batch)} inputs")
            yield from (np.asarray(row.values, dtype=np.float32) for row in rows)

    def close(self) -> None:
        self.client.close()


def create_embedder(site: SiteConfig) -> LocalEmbeddings | GeminiEmbeddings:
    provider = site.embedding_provider.casefold()
    if provider == "local":
        return LocalEmbeddings()
    if provider == "gemini":
        return GeminiEmbeddings(site.gemini_api_key.get_secret_value(), site.embedding_model)
    raise EmbeddingError(f"Unknown embedding provider {provider!r}. Supported: gemini, local")
