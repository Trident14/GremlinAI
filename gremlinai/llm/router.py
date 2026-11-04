"""
GremlinAI — LLM router.

Routes calls by role (fast | vision) with per-role provider overrides.
Records per-call metrics and enforces the per-run cost budget.
"""
from __future__ import annotations
import json
import logging
import time
from pathlib import Path
from typing import Type

from pydantic import BaseModel

from gremlinai.config import Settings
from gremlinai.llm.client import LLMClient, OpenAICompatClient
from gremlinai.models import RunMetrics

logger = logging.getLogger(__name__)

# Load pricing table at import time
_PRICING_PATH = Path(__file__).parent.parent.parent / "pricing.json"
try:
    _PRICING: dict = json.loads(_PRICING_PATH.read_text())
except Exception:
    _PRICING = {}


_WARNED_UNPRICED: set[str] = set()


def price_for(model: str) -> dict | None:
    """Look up pricing for a model (case-insensitive, ignores a 'meta/' style prefix)."""
    key = model.strip().lower()
    entry = _PRICING.get(model) or _PRICING.get(key) or _PRICING.get(key.split("/")[-1])
    return entry if isinstance(entry, dict) else None


def _cost(model: str, in_tokens: int, out_tokens: int) -> float | None:
    entry = price_for(model)
    if not entry:
        if model not in _WARNED_UNPRICED:
            _WARNED_UNPRICED.add(model)
            logger.warning("No pricing for model %r in pricing.json - cost will show as n/a", model)
        return None
    return (in_tokens / 1_000_000) * entry["input_per_1m"] + \
           (out_tokens / 1_000_000) * entry["output_per_1m"]


def _build_client(settings: Settings, provider: str) -> LLMClient:
    if provider == "mock":
        from gremlinai.llm.mock import MockLLMClient
        return MockLLMClient()
    if provider == "gemini":
        return OpenAICompatClient(
            base_url=settings.gemini_base_url,
            api_key=settings.gemini_api_key,
            fast_model=settings.gemini_model_fast,
            vision_model=settings.gemini_model_vision,
            rpm=settings.llm_max_rpm,
            timeout_s=settings.llm_timeout_s,
        )
    if provider == "meta":
        return OpenAICompatClient(
            base_url=settings.meta_api_base,
            api_key=settings.meta_api_key,
            fast_model=settings.meta_model_fast,
            vision_model=settings.meta_model_vision,
            rpm=settings.llm_max_rpm,
            timeout_s=settings.llm_timeout_s,
            effort_fast=settings.llm_reasoning_effort_fast,
            effort_vision=settings.llm_reasoning_effort_vision,
            max_output_tokens=settings.llm_max_output_tokens,
        )
    raise ValueError(f"Unknown LLM provider: {provider!r}")


class CallMetric(BaseModel):
    role: str
    model: str
    latency_s: float
    in_tokens: int = 0
    out_tokens: int = 0
    cost_usd: float | None = None


class LLMRouter:
    """
    Single entry point for all LLM calls.
    Enforces cost budget; surfaces metrics per run.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

        fast_prov  = settings.llm_fast_provider   or settings.llm_provider
        vision_prov = settings.llm_vision_provider or settings.llm_provider

        self._fast   = _build_client(settings, fast_prov)
        self._vision = _build_client(settings, vision_prov)

        self._metrics: list[CallMetric] = []
        self._total_cost: float = 0.0
        self._budget_exceeded = False

    # ── Public call methods ───────────────────────────────────────────────────

    async def fast(
        self, system: str, user: str, schema: Type[BaseModel]
    ) -> BaseModel:
        self._check_budget()
        t0 = time.monotonic()
        result = await self._fast.complete_json("fast", system, user, schema)
        self._record("fast", self._fast.fast_model, time.monotonic() - t0, self._fast)
        return result

    async def vision(
        self, system: str, user: str, image_bytes: bytes, schema: Type[BaseModel]
    ) -> BaseModel:
        self._check_budget()
        t0 = time.monotonic()
        result = await self._vision.complete_vision_json(
            "vision", system, user, image_bytes, schema
        )
        self._record("vision", self._vision.vision_model, time.monotonic() - t0, self._vision)
        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    def budget_exceeded(self) -> bool:
        """True once the cost budget is used up (never raises)."""
        try:
            self._check_budget()
            return False
        except RuntimeError:
            return True

    def _check_budget(self) -> None:
        if self._budget_exceeded:
            raise RuntimeError("budget_exceeded")
        if self._total_cost > self._settings.max_cost_usd_per_run:
            self._budget_exceeded = True
            raise RuntimeError("budget_exceeded")

    def _record(self, role: str, model: str, latency_s: float, client=None) -> None:
        in_tok, out_tok = (0, 0)
        if client is not None and hasattr(client, "pop_usage"):
            in_tok, out_tok = client.pop_usage()
        m = CallMetric(
            role=role, model=model, latency_s=latency_s,
            in_tokens=in_tok, out_tokens=out_tok,
            cost_usd=_cost(model, in_tok, out_tok),
        )
        self._metrics.append(m)
        if m.cost_usd is not None:
            self._total_cost += m.cost_usd

    def aggregate(self) -> RunMetrics:
        fast_calls   = sum(1 for m in self._metrics if m.role == "fast")
        vision_calls = sum(1 for m in self._metrics if m.role == "vision")
        all_latencies = [m.latency_s * 1000 for m in self._metrics]
        avg_ms = (sum(all_latencies) / len(all_latencies)) if all_latencies else 0.0

        total_cost: float | None = self._total_cost if any(
            m.cost_usd is not None for m in self._metrics
        ) else None

        return RunMetrics(
            fast_calls=fast_calls,
            vision_calls=vision_calls,
            avg_latency_ms=round(avg_ms, 1),
            total_cost_usd=round(total_cost, 6) if total_cost is not None else None,
            provider=self._settings.llm_provider,
            fast_model=self._fast.fast_model,
            vision_model=self._vision.vision_model,
        )

    async def aclose(self) -> None:
        if hasattr(self._fast, "aclose"):
            await self._fast.aclose()
        if self._vision is not self._fast and hasattr(self._vision, "aclose"):
            await self._vision.aclose()
