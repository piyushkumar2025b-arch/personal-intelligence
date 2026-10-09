"""Pure-logic tests: no databases, no network, no model downloads."""

import os
import sys
import types

os.environ.setdefault("OPENROUTER_API_KEY", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")

import pytest

from personal_intelligence.core.graph.client import _lucene_escape
from personal_intelligence.core.rag.ingest import split_text
from personal_intelligence.core.rag.retriever import (
    RetrievedContext, _reciprocal_rank_fusion, _tokenize,
)


def test_rrf_rewards_agreement_across_lists():
    scores = _reciprocal_rank_fusion([["a", "b", "c"], ["b", "a"], ["b"]])
    assert max(scores, key=scores.get) == "b"
    assert set(scores) == {"a", "b", "c"}


def test_rrf_empty_lists():
    assert _reciprocal_rank_fusion([[], []]) == {}


def test_tokenize_strips_punctuation():
    assert _tokenize("RAG, graphs & Qdrant!") == ["rag", "graphs", "qdrant"]


def test_lucene_escape_special_chars():
    assert _lucene_escape("what is C++?") == r"what is C\+\+\?"
    assert _lucene_escape("plain words") == "plain words"


def test_split_text_overlap_and_coverage():
    text = " ".join(f"word{i}" for i in range(2000))
    chunks = split_text(text, size=100, overlap=20)
    assert len(chunks) > 1
    assert chunks[0].split()[-1] in chunks[1]  # overlap carries context forward


def test_split_text_rejects_bad_overlap():
    with pytest.raises(ValueError):
        split_text("x", size=10, overlap=10)


def test_reranker_orders_by_score(monkeypatch):
    from personal_intelligence.core.rag import retriever as r

    class FakeModel:
        def predict(self, pairs):
            import numpy as np
            return np.array([0.1, 0.9, 0.5])

    rr = r.CrossEncoderReranker()
    rr._model = FakeModel()
    docs = [RetrievedContext(str(i), t, "s", "text", 0.0, "vector") for i, t in enumerate("abc")]
    out = rr.rerank("q", docs)
    assert [d.chunk_id for d in out] == ["1", "2", "0"]
