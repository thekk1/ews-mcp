"""SemanticIndex adapter — the OPTIONAL vector tier (architecture law #2).

The core server ships with zero dependencies beyond Exchange credentials:
``EWS_SEMANTIC_INDEX=none`` (default) means no adapter, no psycopg, no
embedding calls, and the ``find_similar`` tool is not even registered.

``EWS_SEMANTIC_INDEX=pgvector`` activates this adapter: cleaned message
text (chunked ~1k chars) is embedded at sync time via an Ollama server
(``bge-m3``, 1024-d, Arabic-capable — that is why) into a pgvector table.
``search_messages(mode="semantic")`` fuses FTS and cosine ranks with
reciprocal-rank fusion; an outage degrades to keyword-only with
``meta.degraded`` — never an error.

psycopg is intentionally a soft import: it is NOT a project dependency.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Dict, List, Optional, Protocol, Tuple

try:  # optional dependency — module-top guard (fails loud, degrades soft)
    import psycopg
except ImportError:  # pragma: no cover - absence is the default posture
    psycopg = None

logger = logging.getLogger(__name__)

CHUNK_CHARS = 1000
EMBED_DIM = 1024
RRF_K = 60  # standard reciprocal-rank-fusion constant


class SemanticIndex(Protocol):
    def add(self, items: List[Dict[str, str]]) -> None: ...

    def delete(self, ews_ids: List[str]) -> None: ...

    def query(self, text: str, top_k: int = 20) -> List[Tuple[str, float]]: ...

    def query_similar(self, ews_id: str,
                      top_k: int = 5) -> List[Tuple[str, float]]: ...

    def health(self) -> Dict[str, Any]: ...


def rrf_merge(*rankings: List[str], k: int = RRF_K) -> List[str]:
    """Reciprocal-rank fusion over id rankings (best first)."""
    scores: Dict[str, float] = {}
    for ranking in rankings:
        for rank, ews_id in enumerate(ranking):
            scores[ews_id] = scores.get(ews_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=scores.get, reverse=True)


def chunk_text(text: str, size: int = CHUNK_CHARS) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    return [text[i:i + size] for i in range(0, len(text), size)]


class PgVectorSemanticIndex:
    """pgvector-backed index; embeddings from an Ollama server."""

    def __init__(self, dsn: str, ollama_url: str, model: str = "bge-m3"):
        if psycopg is None:
            raise RuntimeError(
                "EWS_SEMANTIC_INDEX=pgvector requires the 'psycopg' package "
                "(pip install psycopg[binary]) — it is deliberately not a "
                "core dependency."
            )
        self.dsn = dsn
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self._ensure_schema()

    # ------------------------------------------------------------ plumbing

    def _conn(self):
        return psycopg.connect(self.dsn, autocommit=True)

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("CREATE SCHEMA IF NOT EXISTS ews")
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ews.message_embeddings (
                    ews_id   text NOT NULL,
                    chunk    int  NOT NULL,
                    embedding vector({EMBED_DIM}),
                    PRIMARY KEY (ews_id, chunk)
                )
                """
            )

    def _embed(self, texts: List[str]) -> List[List[float]]:
        payload = json.dumps({"model": self.model, "input": texts}).encode()
        req = urllib.request.Request(
            f"{self.ollama_url}/api/embed", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return data["embeddings"]

    # ------------------------------------------------------------- protocol

    def health(self) -> Dict[str, Any]:
        try:
            with self._conn() as conn:
                n = conn.execute(
                    "SELECT COUNT(DISTINCT ews_id) FROM ews.message_embeddings"
                ).fetchone()[0]
            self._embed(["ping"])
            return {"ok": True, "indexed_messages": int(n)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def add(self, items: List[Dict[str, str]]) -> None:
        for item in items:
            chunks = chunk_text(item.get("text", ""))
            if not chunks:
                continue
            vectors = self._embed(chunks)
            with self._conn() as conn:
                conn.execute(
                    "DELETE FROM ews.message_embeddings WHERE ews_id = %s",
                    (item["ews_id"],),
                )
                for i, vec in enumerate(vectors):
                    conn.execute(
                        "INSERT INTO ews.message_embeddings (ews_id, chunk, "
                        "embedding) VALUES (%s, %s, %s)",
                        (item["ews_id"], i, vec),
                    )

    def delete(self, ews_ids: List[str]) -> None:
        if not ews_ids:
            return
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM ews.message_embeddings WHERE ews_id = ANY(%s)",
                (ews_ids,),
            )

    def query(self, text: str, top_k: int = 20) -> List[Tuple[str, float]]:
        vec = self._embed([text])[0]
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ews_id, MIN(embedding <=> %s::vector) AS dist "
                "FROM ews.message_embeddings GROUP BY ews_id "
                "ORDER BY dist ASC LIMIT %s",
                (vec, top_k),
            ).fetchall()
        return [(r[0], 1.0 - float(r[1])) for r in rows]

    def query_similar(self, ews_id: str,
                      top_k: int = 5) -> List[Tuple[str, float]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT o.ews_id, MIN(o.embedding <=> s.embedding) AS dist
                FROM ews.message_embeddings o,
                     ews.message_embeddings s
                WHERE s.ews_id = %s AND s.chunk = 0 AND o.ews_id <> %s
                GROUP BY o.ews_id ORDER BY dist ASC LIMIT %s
                """,
                (ews_id, ews_id, top_k),
            ).fetchall()
        return [(r[0], 1.0 - float(r[1])) for r in rows]


def build_semantic_index(settings: Any) -> Optional[SemanticIndex]:
    """None for the default core; a pgvector adapter when configured.
    Construction failure logs and returns None — the server never gates
    on the optional tier."""
    if getattr(settings, "ews_semantic_index", "none") != "pgvector":
        return None
    dsn = getattr(settings, "ews_semantic_pg_dsn", None)
    if not dsn:
        logger.error("EWS_SEMANTIC_INDEX=pgvector but EWS_SEMANTIC_PG_DSN "
                     "is unset — semantic tier disabled")
        return None
    try:
        return PgVectorSemanticIndex(
            dsn=dsn,
            ollama_url=getattr(settings, "ews_semantic_ollama_url",
                               "http://localhost:11434"),
            model=getattr(settings, "ews_semantic_model", "bge-m3"),
        )
    except Exception as exc:
        logger.error("semantic index init failed (%s) — keyword-only", exc)
        return None
