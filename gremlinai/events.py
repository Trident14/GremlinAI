"""
GremlinAI — async event bus.
Every agent component publishes structured events; the WebSocket handler
and report builders consume the same stream.
"""
from __future__ import annotations
import asyncio
from enum import Enum
from typing import Any
from pydantic import BaseModel


class EventType(str, Enum):
    PLAN_CREATED    = "plan_created"
    STEP_STARTED    = "step_started"
    FRAME           = "frame"            # base64 JPEG — live browser view
    ACTION_EXECUTED = "action_executed"
    VERDICT         = "verdict"
    BUG_LOGGED      = "bug_logged"
    STEP_FINISHED   = "step_finished"
    METRICS         = "metrics"
    RUN_FINISHED    = "run_finished"
    RUN_ERROR       = "run_error"


class Event(BaseModel):
    type: EventType
    run_id: str
    data: Any = None


class EventBus:
    """
    Per-run asyncio queue fan-out to WebSocket subscribers.
    Publishing never blocks the agent — full queues drop frames silently.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._queues.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        subs = self._queues.get(run_id, [])
        if q in subs:
            subs.remove(q)

    async def publish(self, event: Event) -> None:
        for q in self._queues.get(event.run_id, []):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop frame — never block the agent loop

    async def publish_simple(self, run_id: str, etype: EventType, data: Any = None) -> None:
        await self.publish(Event(type=etype, run_id=run_id, data=data))


# Singleton — imported everywhere
event_bus = EventBus()
