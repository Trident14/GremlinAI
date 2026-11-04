"""
GremlinAI — MockLLMClient for offline development and tests.

Feed it scripted responses via the responses queue or override defaults.
All tests import this and set LLM_PROVIDER=mock.
"""
from __future__ import annotations
import copy
from typing import Any, Type
from pydantic import BaseModel

from gremlinai.llm.client import LLMClient
from gremlinai.models import (
    TestPlan, Criterion, TestStep,
    Action, Verdict,
)


# Default scripted fallbacks per schema type
_DEFAULTS: dict[str, Any] = {
    "TestPlan": {
        "story": "Mock story",
        "criteria": [{"id": "AC1", "text": "Mock criterion"}],
        "steps": [
            {
                "id": "S1",
                "criterion_id": "AC1",
                "instruction": "Navigate to the page",
                "expected": "The page is visible",
            }
        ],
    },
    "Action": {
        "type": "done",
        "element_idx": None,
        "text": None,
        "reasoning": "Mock: step already complete",
    },
    "Verdict": {
        "status": "pass",
        "evidence": "Mock: expected outcome observed",
        "confidence": 1.0,
    },
}

_BUG_TITLE_DEFAULT = {
    "title": "Mock bug title",
    "severity": "major",
}


class MockLLMClient(LLMClient):
    """
    Returns scripted responses in order; falls back to schema-specific defaults.
    Useful for unit tests and offline pipeline development.
    """

    def __init__(self, responses: list[dict] | None = None) -> None:
        self._queue: list[dict] = list(responses or [])
        self.calls: list[dict] = []   # recorded for assertions in tests

    def push(self, response: dict) -> None:
        """Queue one more scripted response."""
        self._queue.append(response)

    async def complete_json(
        self, role: str, system: str, user: str, schema: Type[BaseModel]
    ) -> BaseModel:
        self.calls.append({"role": role, "type": "text", "schema": schema.__name__})
        raw = self._next(schema)
        return schema.model_validate(raw)

    async def complete_vision_json(
        self,
        role: str,
        system: str,
        user: str,
        image_bytes: bytes,
        schema: Type[BaseModel],
    ) -> BaseModel:
        self.calls.append({"role": role, "type": "vision", "schema": schema.__name__})
        raw = self._next(schema)
        return schema.model_validate(raw)

    def _next(self, schema: Type[BaseModel]) -> dict:
        if self._queue:
            return self._queue.pop(0)
        name = schema.__name__
        if name in _DEFAULTS:
            return copy.deepcopy(_DEFAULTS[name])
        # Generic fallback — return empty-ish valid dict
        return {}

    @property
    def fast_model(self) -> str:
        return "mock-fast"

    @property
    def vision_model(self) -> str:
        return "mock-vision"
