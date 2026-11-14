"""Domain RAG over strategic principles (no concrete moves are ever indexed).

Embeddings use a deterministic feature-hashing encoder so the stack works
offline; swap ``HashingEmbedder`` for a sentence-transformer by implementing
``embed``. Two stores share the interface:

* ``MemoryStore``   - cosine similarity in numpy (default, tests)
* ``PgVectorStore`` - PostgreSQL + pgvector (``COGNITO_RAG_BACKEND=pgvector``)
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import yaml

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
DIM = 512


@dataclass(frozen=True)
class Chunk:
    id: str
    title: str
    tags: tuple[str, ...]
    text: str
    question: str


class HashingEmbedder:
    dim = DIM

    def embed(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        toks = re.findall(r"[a-z_]+", text.lower())
        for tok in toks + [f"{a}|{b}" for a, b in zip(toks, toks[1:])]:
            h = int(hashlib.blake2b(tok.encode(), digest_size=8).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        n = np.linalg.norm(v)
        return v / n if n else v


def _doc_text(c: Chunk) -> str:
    # tags are repeated so structural tags dominate similarity
    return " ".join(list(c.tags) * 3) + " " + c.title + " " + c.text


@lru_cache
def load_chunks() -> tuple[Chunk, ...]:
    out = []
    for p in sorted(KNOWLEDGE_DIR.glob("*.yaml")):
        for d in yaml.safe_load(p.read_text()):
            out.append(Chunk(d["id"], d["title"], tuple(d["tags"]), d["text"], d["question"]))
    return tuple(out)


class MemoryStore:
    def __init__(self, embedder: HashingEmbedder | None = None) -> None:
        self.embedder = embedder or HashingEmbedder()
        self.chunks = load_chunks()
        self.matrix = np.stack([self.embedder.embed(_doc_text(c)) for c in self.chunks])

    async def search(self, query: str, k: int = 3, tags: list[str] | None = None) -> list[Chunk]:
        q = self.embedder.embed(query)
        sims = self.matrix @ q
        if tags:
            boost = np.array([len(set(tags) & set(c.tags)) for c in self.chunks], dtype=np.float32)
            sims = sims + 0.25 * boost
        idx = np.argsort(-sims)[:k]
        return [self.chunks[i] for i in idx]


class PgVectorStore:  # pragma: no cover - exercised in docker-compose
    TABLE = "rag_chunks"

    def __init__(self, sessionmaker, embedder: HashingEmbedder | None = None) -> None:
        self.sm = sessionmaker
        self.embedder = embedder or HashingEmbedder()
        self._ready = False

    async def _ensure(self) -> None:
        if self._ready:
            return
        from sqlalchemy import text

        async with self.sm() as s:
            await s.execute(text(f"CREATE TABLE IF NOT EXISTS {self.TABLE} (id text primary key, title text, "
                                 f"tags text[], body text, question text, embedding vector({DIM}))"))
            for c in load_chunks():
                vec = "[" + ",".join(f"{x:.6f}" for x in self.embedder.embed(_doc_text(c))) + "]"
                await s.execute(text(f"INSERT INTO {self.TABLE} VALUES (:i,:t,:g,:b,:q,CAST(:e AS vector)) "
                                     "ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding"),
                                {"i": c.id, "t": c.title, "g": list(c.tags), "b": c.text, "q": c.question, "e": vec})
            await s.commit()
        self._ready = True

    async def search(self, query: str, k: int = 3, tags: list[str] | None = None) -> list[Chunk]:
        from sqlalchemy import text

        await self._ensure()
        vec = "[" + ",".join(f"{x:.6f}" for x in self.embedder.embed(query + " " + " ".join(tags or []))) + "]"
        async with self.sm() as s:
            rows = (await s.execute(text(f"SELECT id,title,tags,body,question FROM {self.TABLE} "
                                         "ORDER BY embedding <=> CAST(:e AS vector) LIMIT :k"), {"e": vec, "k": k})).all()
        return [Chunk(r[0], r[1], tuple(r[2]), r[3], r[4]) for r in rows]


_store = None


def get_store():
    global _store
    if _store is None:
        from ..config import get_settings

        if get_settings().rag_backend == "pgvector":
            from ..db import sessionmaker

            _store = PgVectorStore(sessionmaker())
        else:
            _store = MemoryStore()
    return _store
