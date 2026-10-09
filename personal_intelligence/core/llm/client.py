"""
OpenRouter client — wraps the OpenAI-compatible API.
Handles: model selection, cost tracking, retries, streaming, token counting.
"""

import os
import time
from typing import AsyncIterator, Any
from dataclasses import dataclass, field

try:
    import tiktoken
    HAS_TIKTOKEN = True
except ImportError:
    tiktoken = None  # type: ignore
    HAS_TIKTOKEN = False
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, InternalServerError, RateLimitError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from personal_intelligence.config.settings import settings
from personal_intelligence.infrastructure.monitoring.logger import get_logger
from personal_intelligence.infrastructure.monitoring.metrics import LLM_REQUESTS, LLM_TOKENS, LLM_LATENCY

logger = get_logger(__name__)

# ── Model registry ───────────────────────────────────────────────────────────

MODEL_COSTS: dict[str, dict[str, float]] = {
    # model_id: {input: $/1M tokens, output: $/1M tokens}
    "nvidia/nemotron-3-super-120b-a12b:free": {"input": 0.0, "output": 0.0},
    "liquid/lfm-2.5-2.6b:free": {"input": 0.0, "output": 0.0},
    "openrouter/free": {"input": 0.0, "output": 0.0},
    "anthropic/claude-3.5-sonnet": {"input": 3.0, "output": 15.0},
    "anthropic/claude-3-haiku": {"input": 0.25, "output": 1.25},
    "openai/gpt-4o": {"input": 5.0, "output": 15.0},
    "openai/gpt-4o-mini": {"input": 0.15, "output": 0.6},
    "meta-llama/llama-3.1-70b-instruct": {"input": 0.35, "output": 0.4},
    "meta-llama/llama-3.1-8b-instruct": {"input": 0.05, "output": 0.05},
    "google/gemini-pro-1.5": {"input": 1.25, "output": 5.0},
    "mistralai/mixtral-8x7b-instruct": {"input": 0.24, "output": 0.24},
    "openai/text-embedding-3-small": {"input": 0.02, "output": 0.0},
}


@dataclass
class LLMResponse:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    finish_reason: str = "stop"
    raw: Any = field(default=None, repr=False)


@dataclass
class CostTracker:
    """Session-level cost accumulator."""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    calls: int = 0

    def record(self, response: LLMResponse) -> None:
        self.total_input_tokens += response.input_tokens
        self.total_output_tokens += response.output_tokens
        self.total_cost_usd += response.cost_usd
        self.calls += 1


def _calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    if ":free" in model or model == "openrouter/free":
        return 0.0
    costs = MODEL_COSTS.get(model, {"input": 1.0, "output": 3.0})
    return (input_tokens * costs["input"] + output_tokens * costs["output"]) / 1_000_000


def _count_tokens(text: str, model: str = "gpt-4o") -> int:
    if not HAS_TIKTOKEN or tiktoken is None:
        return len(text.split())
    try:
        enc = tiktoken.encoding_for_model(model)
    except KeyError:
        enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))


class OpenRouterClient:
    """
    Async OpenRouter client.
    - Pluggable models (smart / fast tiers)
    - Automatic retry with exponential backoff
    - Per-call and session cost tracking
    - Prometheus metrics
    """

    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self.cost_tracker = CostTracker()

    def _get_api_key(self) -> str:
        key = (
            os.environ.get("OPENROUTER_API_KEY")
            or settings.openrouter_api_key
        ).strip()
        if not key or key == "your-openrouter-key-here":
            raise ValueError(
                "OPENROUTER_API_KEY is missing or invalid! "
                "Please set your real OpenRouter key: os.environ['OPENROUTER_API_KEY'] = 'sk-or-v1-...'"
            )
        return key

    @property
    def client(self) -> AsyncOpenAI:
        key = self._get_api_key()
        if self._client is None or getattr(self._client, "api_key", None) != key:
            self._client = AsyncOpenAI(
                api_key=key,
                base_url=settings.openrouter_base_url,
                default_headers={
                    "HTTP-Referer": settings.openrouter_site_url,
                    "X-Title": settings.openrouter_site_name,
                },
            )
        return self._client

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)),
        reraise=True,
    )
    async def complete(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system: str | None = None,
        response_format: dict | None = None,
    ) -> LLMResponse:
        model = model or settings.openrouter_default_model
        temperature = temperature if temperature is not None else settings.agent_temperature
        max_tokens = max_tokens or settings.max_output_tokens

        if system:
            messages = [{"role": "system", "content": system}] + messages

        kwargs: dict[str, Any] = dict(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if response_format:
            kwargs["response_format"] = response_format

        t0 = time.perf_counter()
        try:
            resp = await self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            LLM_REQUESTS.labels(model=model, status="error").inc()
            logger.error("llm_error", model=model, error=str(exc))
            raise

        latency_ms = (time.perf_counter() - t0) * 1000
        usage = resp.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        cost = _calculate_cost(model, input_tokens, output_tokens)

        result = LLMResponse(
            content=(resp.choices[0].message.content or "") if resp.choices else "",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            finish_reason=(resp.choices[0].finish_reason or "stop") if resp.choices else "error",
            raw=resp,
        )

        self.cost_tracker.record(result)
        LLM_REQUESTS.labels(model=model, status="ok").inc()
        LLM_TOKENS.labels(model=model, direction="input").inc(input_tokens)
        LLM_TOKENS.labels(model=model, direction="output").inc(output_tokens)
        LLM_LATENCY.labels(model=model).observe(latency_ms / 1000)

        logger.debug(
            "llm_complete",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(cost, 6),
            latency_ms=round(latency_ms, 1),
        )
        return result

    async def complete_fast(self, messages: list[dict], **kwargs) -> LLMResponse:
        """Use the fast/cheap model (for classification, routing, etc.)."""
        return await self.complete(messages, model=settings.openrouter_fast_model, **kwargs)

    async def stream(
        self,
        messages: list[dict],
        model: str | None = None,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        model = model or settings.openrouter_default_model
        if system:
            messages = [{"role": "system", "content": system}] + messages

        async with await self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=settings.agent_temperature,
            stream=True,
        ) as stream:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch embed texts via OpenRouter (OpenAI embedding endpoint)."""
        model = settings.openrouter_embedding_model
        results: list[list[float]] = []

        for i in range(0, len(texts), settings.embedding_batch_size):
            batch = texts[i : i + settings.embedding_batch_size]
            resp = await self.client.embeddings.create(model=model, input=batch)
            results.extend([item.embedding for item in resp.data])

        return results

    def session_cost_summary(self) -> dict:
        t = self.cost_tracker
        return {
            "calls": t.calls,
            "input_tokens": t.total_input_tokens,
            "output_tokens": t.total_output_tokens,
            "total_cost_usd": round(t.total_cost_usd, 6),
        }


# Singleton for the whole app
llm_client = OpenRouterClient()
