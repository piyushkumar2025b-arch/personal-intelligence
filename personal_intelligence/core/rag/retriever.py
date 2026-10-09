"""
Hybrid retriever:
  1. BM25 keyword match over chunk texts
  2. Vector semantic search (Qdrant)
  3. Graph-augmented retrieval (Neo4j entity lookup → chunk IDs → fetch)
  4. Reciprocal Rank Fusion to merge results
  5. Cross-encoder reranker for final ordering
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

try:
    from rank_bm25 import BM25Okapi
    HAS_BM25 = True
except ImportError:
    BM25Okapi = None  # type: ignore
    HAS_BM25 = False

from personal_intelligence.config.settings import settings
from personal_intelligence.core.graph.client import graph_client
from personal_intelligence.core.llm.client import llm_client
from personal_intelligence.core.memory.vector_store import vector_store
from personal_intelligence.infrastructure.db.repositories import ChunkRepository
from personal_intelligence.infrastructure.monitoring.logger import get_logger
from personal_intelligence.infrastructure.monitoring.metrics import RETRIEVAL_LATENCY

if TYPE_CHECKING:  # heavy import (torch); loaded lazily in the reranker
    from sentence_transformers import CrossEncoder

logger = get_logger(__name__)


@dataclass
class RetrievedContext:
    chunk_id: str
    text: str
    source: str
    source_type: str
    score: float
    retrieval_method: str       # vector | bm25 | graph | fused
    entity_context: list[dict] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens. str.split() leaves punctuation glued to words ('rag,' != 'rag')."""
    return _TOKEN_RE.findall(text.lower())


def _reciprocal_rank_fusion(
    rank_lists: list[list[str]],
    k: int = 60,
) -> dict[str, float]:
    """RRF: merge multiple ranked lists into a single score dict."""
    scores: dict[str, float] = {}
    for ranked in rank_lists:
        for rank, doc_id in enumerate(ranked):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


class CrossEncoderReranker:
    def __init__(self) -> None:
        self._model: CrossEncoder | None = None

    def _load(self) -> CrossEncoder:
        if self._model is None:
            logger.info("loading_reranker", model=settings.reranker_model)
            from sentence_transformers import CrossEncoder  # lazy: avoids importing torch at startup

            self._model = CrossEncoder(settings.reranker_model)
        return self._model

    def rerank(self, query: str, docs: list[RetrievedContext]) -> list[RetrievedContext]:
        if not docs:
            return docs
        model = self._load()
        pairs = [(query, d.text) for d in docs]
        scores: list[float] = model.predict(pairs).tolist()
        for doc, score in zip(docs, scores):
            doc.score = float(score)
        docs.sort(key=lambda d: d.score, reverse=True)
        logger.debug("reranker_done", total=len(docs), top_score=round(docs[0].score, 4))
        return docs

    async def arerank(self, query: str, docs: list[RetrievedContext]) -> list[RetrievedContext]:
        """Run the CPU-bound model off the event loop."""
        return await asyncio.to_thread(self.rerank, query, docs)


reranker = CrossEncoderReranker()


class HybridRetriever:
    """
    Full hybrid retrieval pipeline.
    Call: results = await retriever.retrieve(query, top_k=5)
    """

    BM25_CACHE_TTL_S = 60.0

    def __init__(self, chunk_repo: ChunkRepository) -> None:
        self._chunk_repo = chunk_repo
        # Rebuilding BM25 over 2000 chunks on every query is wasteful; cache per source_type.
        self._bm25_cache: dict[str | None, tuple[float, BM25Okapi, list[str]]] = {}

    async def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        source_type: str | None = None,
    ) -> list[RetrievedContext]:
        t_start = time.perf_counter()
        top_k = top_k or settings.top_k_rerank
        k_fetch = settings.top_k_retrieval

        # ── 1. Embed query ────────────────────────────────────────
        [q_emb] = await llm_client.embed([query])

        # ── 2-4. Vector, BM25 and graph search run concurrently ───
        vector_hits, bm25_ids, graph_ids = await asyncio.gather(
            vector_store.search(q_emb, top_k=k_fetch, source_type=source_type),
            self._bm25_search(query, top_k=k_fetch, source_type=source_type),
            self._graph_search(query, top_k=k_fetch),
        )
        vector_ids = [h.chunk.id for h in vector_hits]

        # ── 5. RRF fusion ─────────────────────────────────────────
        fused_scores = _reciprocal_rank_fusion([vector_ids, bm25_ids, graph_ids])
        fused_ids = sorted(fused_scores, key=lambda x: fused_scores[x], reverse=True)[:k_fetch]

        # ── 6. Hydrate chunks from Postgres ───────────────────────
        chunks_map = await self._chunk_repo.get_by_ids(fused_ids)

        # ── 7. Build RetrievedContext objects ─────────────────────
        vector_score_map = {h.chunk.id: h.score for h in vector_hits}
        bm25_set, graph_set = set(bm25_ids), set(graph_ids)
        contexts: list[RetrievedContext] = []
        for cid in fused_ids:
            chunk = chunks_map.get(cid)
            if not chunk:
                continue
            # Graph hits come from Neo4j without a source_type filter; enforce it here.
            if source_type and chunk.source_type != source_type:
                continue
            hits_in = [m for m, ids in (("vector", vector_score_map), ("bm25", bm25_set), ("graph", graph_set))
                       if cid in ids]
            method = hits_in[0] if len(hits_in) == 1 else "fused"
            contexts.append(RetrievedContext(
                chunk_id=cid,
                text=chunk.text,
                source=chunk.source,
                source_type=chunk.source_type,
                score=fused_scores.get(cid, 0.0),
                retrieval_method=method,
                metadata=chunk.metadata or {},
            ))

        # ── 8. Rerank ─────────────────────────────────────────────
        contexts = await reranker.arerank(query, contexts)

        # ── 9. Enrich top results with graph context ───────────────
        contexts = await self._enrich_with_graph(contexts[:top_k])

        RETRIEVAL_LATENCY.observe(time.perf_counter() - t_start)
        logger.info(
            "retrieval_done",
            query_preview=query[:60],
            vector=len(vector_hits),
            bm25=len(bm25_ids),
            graph=len(graph_ids),
            fused=len(fused_ids),
            after_rerank=len(contexts),
        )
        return contexts

    async def _bm25_search(
        self, query: str, top_k: int, source_type: str | None = None
    ) -> list[str]:
        """BM25 over the most recent chunks (rolling window), cached briefly."""
        if not HAS_BM25:
            return []
        try:
            cached = self._bm25_cache.get(source_type)
            if cached and time.monotonic() - cached[0] < self.BM25_CACHE_TTL_S:
                _, bm25, ids = cached
            else:
                recent = await self._chunk_repo.get_recent(limit=2000, source_type=source_type)
                if not recent:
                    return []
                bm25 = await asyncio.to_thread(BM25Okapi, [_tokenize(c.text) or [""] for c in recent])
                ids = [c.id for c in recent]
                self._bm25_cache[source_type] = (time.monotonic(), bm25, ids)

            q_tokens = _tokenize(query)
            if not q_tokens:
                return []
            scores = bm25.get_scores(q_tokens)
            ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            # Drop zero-score docs: they carry no keyword signal and would pollute RRF.
            return [ids[i] for i in ranked[:top_k] if scores[i] > 0]
        except Exception as exc:
            logger.warning("bm25_failed", error=str(exc))
            return []

    async def _graph_search(self, query: str, top_k: int) -> list[str]:
        """Find entities in query → get chunk IDs via graph."""
        try:
            entities = await graph_client.find_entities_by_name(query, limit=5)
            if not entities:
                return []
            entity_ids = [e["id"] for e in entities]
            return await graph_client.get_chunks_for_entities(entity_ids, limit=top_k)
        except Exception as exc:
            logger.warning("graph_search_failed", error=str(exc))
            return []

    async def _enrich_with_graph(self, contexts: list[RetrievedContext]) -> list[RetrievedContext]:
        """Add entity neighborhood context to top results (concurrent, best-effort)."""

        async def enrich(ctx: RetrievedContext) -> None:
            try:
                entities = await graph_client.find_entities_by_name(ctx.text[:100], limit=3)
                for entity in entities[:2]:
                    ctx.entity_context.extend(
                        await graph_client.get_related_entities(entity["id"], limit=5)
                    )
            except Exception as exc:
                logger.debug("graph_enrich_failed", chunk_id=ctx.chunk_id, error=str(exc))

        await asyncio.gather(*(enrich(c) for c in contexts))
        return contexts
