"""Text similarity for dedupe (same event?) and relevance (which seed?).

FinBERT is a *sentiment* model; its vectors do not separate "same event" from "same tone", so it is
not used here. Two interchangeable embedders:

  tfidf                  no dependencies; word + bigram TF-IDF fitted on the run's own corpus.
  sentence_transformers  dense embeddings (requirements-ml.txt); better at paraphrases.

Vectors are either sparse dicts or float lists; `cosine` accepts both.
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from typing import Protocol

from app.config import Settings

log = logging.getLogger(__name__)
_TOKEN = re.compile(r"[a-z0-9&]{2,}")
_STOP = frozenset(
    "the a an and or of in on to for at by with from as is are was were be been it its this that "
    "after over amid says said will may new more than into up down".split()
)

Vector = dict[str, float] | list[float]


class Embedder(Protocol):
    name: str
    kind: str  # "dense" | "sparse"

    def embed(self, texts: list[str]) -> list[Vector]: ...


def tokens(text: str) -> list[str]:
    words = [w for w in _TOKEN.findall(text.lower()) if w not in _STOP]
    return words + [f"{a}_{b}" for a, b in zip(words, words[1:])]


class TfidfEmbedder:
    name = "tfidf"
    kind = "sparse"  # similarity scale differs from dense models, so thresholds are per kind (tuning.yaml)
    fallback_reason = ""  # set when this is a stand-in for a dense model that failed to load

    def embed(self, texts: list[str]) -> list[Vector]:
        corpus = [Counter(tokens(t)) for t in texts]
        n_docs = len(corpus)
        df: Counter[str] = Counter()
        for doc in corpus:
            df.update(doc.keys())
        vectors: list[Vector] = []
        for doc in corpus:
            vec = {w: (1 + math.log(c)) * (math.log((1 + n_docs) / (1 + df[w])) + 1) for w, c in doc.items()}
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            vectors.append({w: v / norm for w, v in vec.items()})
        return vectors


class SentenceTransformerEmbedder:
    kind = "dense"

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # optional dependency

        self.name = f"st:{model_name.split('/')[-1]}"
        self._model = SentenceTransformer(model_name)

    def embed(self, texts: list[str]) -> list[Vector]:
        return [list(map(float, v)) for v in self._model.encode(texts, normalize_embeddings=True)]


class HfMeanPoolEmbedder:
    """Mean-pooled hidden states of any BERT-style encoder (e.g. FinBERT base), L2-normalised.

    FinBERT's classification head is ignored: this uses the encoder only, so it yields a vector, not a sentiment label.
    """

    def __init__(self, model_name: str = "ProsusAI/finbert", max_length: int = 128, batch_size: int = 32) -> None:
        import torch  # noqa: F401  (optional dependency)
        from transformers import AutoModel, AutoTokenizer

        self.name = f"meanpool:{model_name}"
        self.kind = "dense"
        self._tokenizer = AutoTokenizer.from_pretrained(model_name)
        self._model = AutoModel.from_pretrained(model_name).eval()
        self._max_length, self._batch = max_length, batch_size

    def embed(self, texts: list[str]) -> list[Vector]:
        import torch

        out: list[Vector] = []
        for i in range(0, len(texts), self._batch):
            enc = self._tokenizer(texts[i:i + self._batch], padding=True, truncation=True,
                                  max_length=self._max_length, return_tensors="pt")
            with torch.no_grad():
                hidden = self._model(**enc).last_hidden_state
            mask = enc["attention_mask"].unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1)
            pooled = torch.nn.functional.normalize(pooled, dim=1)
            out.extend([list(map(float, row)) for row in pooled])
        return out


def make_embedder(spec: str) -> Embedder:
    """spec: tfidf | finbert | st:<sentence-transformers model> (used by the benchmark and build_embedder)."""
    spec = spec.strip()
    if spec == "tfidf":
        return TfidfEmbedder()
    if spec == "finbert":
        return HfMeanPoolEmbedder("ProsusAI/finbert")
    if spec.startswith("st:"):
        return SentenceTransformerEmbedder(spec[3:])
    raise ValueError(f"unknown embedder spec {spec!r}")


def cosine(a: Vector, b: Vector) -> float:
    if isinstance(a, dict) and isinstance(b, dict):
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b.get(k, 0.0) for k, v in a.items())
    if isinstance(a, list) and isinstance(b, list):
        return sum(x * y for x, y in zip(a, b))
    raise TypeError("cannot compare sparse and dense vectors")


_MODEL_CACHE: dict[str, Embedder] = {}


def build_embedder(settings: Settings) -> Embedder:
    """The configured embedder; if a dense model cannot load, fall back to TF-IDF and say so (`fallback_reason`)."""
    wanted = settings.embedder.strip().lower()
    if wanted in {"sentence_transformers", "st"}:
        key = settings.embedding_model
        if key not in _MODEL_CACHE:  # loading a model is slow: once per process
            try:
                _MODEL_CACHE[key] = SentenceTransformerEmbedder(key)
            except Exception as exc:  # noqa: BLE001 - fall back rather than fail the run
                log.warning("sentence_transformers unavailable (%s); falling back to tfidf", exc)
                fallback = TfidfEmbedder()
                fallback.fallback_reason = f"{type(exc).__name__}: {str(exc)[:80]}"
                return fallback
        return _MODEL_CACHE[key]
    if wanted in {"finbert", "meanpool"}:
        key = "finbert"
        if key not in _MODEL_CACHE:
            _MODEL_CACHE[key] = HfMeanPoolEmbedder("ProsusAI/finbert")
        return _MODEL_CACHE[key]
    return TfidfEmbedder()
