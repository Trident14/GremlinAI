"""
GremlinAI — LLM client interface + OpenAI-compatible adapter.

All provider specifics live here. The rest of the app talks only through
LLMClient (via llm/router.py).
"""
from __future__ import annotations
import asyncio
import base64
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Type

import httpx
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


class LLMTimeoutError(RuntimeError):
    """Raised when an LLM provider fails to respond within the deadline."""
    pass


# ── Interface ─────────────────────────────────────────────────────────────────

class LLMClient(ABC):
    """
    Two methods only. Every caller uses these; no provider bleed-through.
    """

    @abstractmethod
    async def complete_json(
        self,
        role: str,
        system: str,
        user: str,
        schema: Type[BaseModel],
    ) -> BaseModel: ...

    @abstractmethod
    async def complete_vision_json(
        self,
        role: str,
        system: str,
        user: str,
        image_bytes: bytes,
        schema: Type[BaseModel],
    ) -> BaseModel: ...


# ── Token-bucket rate limiter ─────────────────────────────────────────────────

class _TokenBucket:
    """Simple token bucket to enforce LLM_MAX_RPM."""

    def __init__(self, rpm: int) -> None:
        self._capacity = max(rpm, 1)
        self._tokens = float(self._capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(
                self._capacity,
                self._tokens + elapsed * (self._capacity / 60.0),
            )
            self._last = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / (self._capacity / 60.0)
                logger.debug("Rate limit: waiting %.1fs", wait)
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


# ── JSON parsing helpers ──────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    """Strip ```json ... ``` or ``` ... ``` wrappers."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_model(raw: str, schema: Type[BaseModel]) -> BaseModel:
    cleaned = _strip_fences(raw)
    data = json.loads(cleaned)
    return schema.model_validate(data)


# ── OpenAI-compatible adapter ─────────────────────────────────────────────────

_BACKOFF = [1.0, 2.0, 4.0, 8.0]  # seconds between retries on 429/5xx


class OpenAICompatClient(LLMClient):
    """
    Speaks the OpenAI chat-completions API.
    Works unchanged for Gemini (v1beta/openai endpoint) and Meta (OAI-compatible).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        fast_model: str,
        vision_model: str,
        rpm: int = 10,
        timeout_s: float = 90.0,
        effort_fast: str | None = None,
        effort_vision: str | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._fast_model = fast_model
        self._vision_model = vision_model
        self._usage_in = 0
        self._usage_out = 0
        self._bucket = _TokenBucket(rpm)
        self._timeout_s = timeout_s
        self._effort_fast = effort_fast or None
        self._effort_vision = effort_vision or None
        self._max_output_tokens = max_output_tokens
        self._extras_disabled = False   # set if the provider rejects reasoning_effort
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=15.0))

    async def complete_json(
        self, role: str, system: str, user: str, schema: Type[BaseModel]
    ) -> BaseModel:
        schema_hint = json.dumps(schema.model_json_schema())
        sys_msg = f"{system}\n\nYou must return ONLY valid JSON matching this schema:\n{schema_hint}"
        messages = [
            {"role": "system", "content": sys_msg},
            {"role": "user",   "content": user},
        ]
        return await self._call(messages, self._fast_model, schema, effort=self._effort_fast)

    async def complete_vision_json(
        self,
        role: str,
        system: str,
        user: str,
        image_bytes: bytes,
        schema: Type[BaseModel],
    ) -> BaseModel:
        b64 = base64.b64encode(image_bytes).decode()
        data_url = f"data:image/jpeg;base64,{b64}"
        schema_hint = json.dumps(schema.model_json_schema())
        sys_msg = f"{system}\n\nYou must return ONLY valid JSON matching this schema:\n{schema_hint}"
        messages = [
            {"role": "system", "content": sys_msg},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        return await self._call(messages, self._vision_model, schema, effort=self._effort_vision)

    async def _call(
        self,
        messages: list[dict],
        model: str,
        schema: Type[BaseModel],
        _attempt: int = 0,
        effort: str | None = None,
    ) -> BaseModel:
        await self._bucket.acquire()

        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        if effort and not self._extras_disabled:
            payload["reasoning_effort"] = effort
            if self._max_output_tokens:
                payload["max_completion_tokens"] = self._max_output_tokens
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

        try:
            resp = await self._http.post(
                f"{self._base}/chat/completions",
                json=payload,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            if _attempt < 1:
                logger.warning("LLM request timed out after %.0fs, retrying once...", self._timeout_s)
                await asyncio.sleep(1.0)
                return await self._call(messages, model, schema, _attempt + 1, effort)
            raise LLMTimeoutError(
                f"LLM provider is not responding (timed out after {self._timeout_s:.0f}s for model '{model}')."
            ) from exc
        except httpx.RequestError as exc:
            if _attempt < len(_BACKOFF):
                await asyncio.sleep(_BACKOFF[_attempt])
                return await self._call(messages, model, schema, _attempt + 1, effort)
            raise

        if resp.status_code == 400 and effort and not self._extras_disabled:
            body_l = resp.text.lower()
            if any(k in body_l for k in ("reasoning", "max_completion", "max_tokens", "unsupported", "unknown")):
                logger.warning("Provider rejected reasoning_effort/max_completion_tokens (%s) - retrying without them",
                               resp.text[:200])
                self._extras_disabled = True
                return await self._call(messages, model, schema, _attempt, effort)

        if resp.status_code == 404:
            err_body = resp.text
            raise RuntimeError(
                f"LLM model '{model}' not found (HTTP 404) at {self._base}. "
                f"The model may be deprecated or misspelled. Check the model name in your .env. Details: {err_body}"
            )

        if resp.status_code in (429, 500, 502, 503, 504):
            if _attempt < len(_BACKOFF):
                wait = _BACKOFF[_attempt]
                logger.warning("HTTP %d — retrying in %.0fs", resp.status_code, wait)
                await asyncio.sleep(wait)
                return await self._call(messages, model, schema, _attempt + 1, effort)
            resp.raise_for_status()

        resp.raise_for_status()
        _data = resp.json()
        raw = _data["choices"][0]["message"]["content"]
        _usage = _data.get("usage") or {}
        self._usage_in += int(_usage.get("prompt_tokens", 0) or 0)
        self._usage_out += int(_usage.get("completion_tokens", 0) or 0)

        # Parse — retry once with the validation error appended
        try:
            return _parse_model(raw, schema)
        except (json.JSONDecodeError, ValidationError, KeyError) as exc:
            if _attempt == 0:
                logger.warning("JSON parse failed (%s) — retrying with error hint", exc)
                repair_msg = (
                    f"Your previous response could not be parsed.\n"
                    f"Error: {exc}\n"
                    f"Raw response: {raw}\n"
                    f"Return ONLY valid JSON matching the schema. No markdown, no code fences."
                )
                messages = messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user",      "content": repair_msg},
                ]
                return await self._call(messages, model, schema, _attempt + 1, effort)
            raise

    def pop_usage(self) -> tuple[int, int]:
        """Return (input_tokens, output_tokens) used since the last call, then reset."""
        usage = (self._usage_in, self._usage_out)
        self._usage_in = self._usage_out = 0
        return usage

    @property
    def fast_model(self) -> str:
        return self._fast_model

    @property
    def vision_model(self) -> str:
        return self._vision_model

    async def aclose(self) -> None:
        await self._http.aclose()
