"""Prometheus metrics shared across the app with fallback."""

try:
    from prometheus_client import Counter, Histogram
    HAS_PROMETHEUS = True
except ImportError:
    HAS_PROMETHEUS = False

    class _DummyMetric:
        def labels(self, *args, **kwargs):
            return self

        def inc(self, *args, **kwargs):
            pass

        def observe(self, *args, **kwargs):
            pass

    def Counter(*args, **kwargs):  # type: ignore
        return _DummyMetric()

    def Histogram(*args, **kwargs):  # type: ignore
        return _DummyMetric()


LLM_REQUESTS = Counter("pi_llm_requests_total", "LLM requests", ["model", "status"])
LLM_TOKENS = Counter("pi_llm_tokens_total", "LLM tokens used", ["model", "direction"])
LLM_LATENCY = Histogram(
    "pi_llm_latency_seconds",
    "LLM request latency",
    ["model"],
    buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 32, 60),
)
RETRIEVAL_LATENCY = Histogram(
    "pi_retrieval_latency_seconds",
    "End-to-end hybrid retrieval latency",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8),
)
