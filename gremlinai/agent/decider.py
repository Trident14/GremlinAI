"""
GremlinAI — decider.

Chooses the next browser action for a given test step.
Guardrails:
- Validates element_idx is in the grounded list
- Detects action loops → escalates to vision model
- Returns cannot_proceed if stuck
"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING

from gremlinai.llm.router import LLMRouter
from gremlinai.llm.prompts import decider_system
from gremlinai.models import Action, ElementRef, TestStep

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _build_user_prompt(
    step: TestStep,
    elements: list[ElementRef],
    visible_text: str,
    history: list[Action],
    current_url: str,
) -> str:
    from gremlinai.agent.grounding import format_elements_for_prompt

    element_str = format_elements_for_prompt(elements, visible_text)
    history_str = "\n".join(
        f"  - {a.type} idx={a.element_idx} text={a.text!r}: {a.reasoning}"
        for a in history[-3:]
    ) or "  (none yet)"

    return (
        f"STEP INSTRUCTION: {step.instruction}\n"
        f"EXPECTED OUTCOME: {step.expected}\n"
        f"CURRENT URL: {current_url}\n\n"
        f"RECENT ACTIONS:\n{history_str}\n\n"
        f"INTERACTIVE ELEMENTS:\n{element_str}\n\n"
        f"Choose the single best next action as JSON."
    )


def _is_loop(history: list[Action]) -> bool:
    """Return True if the last two actions are identical (type + element_idx)."""
    if len(history) < 2:
        return False
    a, b = history[-2], history[-1]
    return a.type == b.type and a.element_idx == b.element_idx


async def decide(
    step: TestStep,
    elements: list[ElementRef],
    visible_text: str,
    history: list[Action],
    current_url: str,
    router: LLMRouter,
    screenshot_bytes: bytes | None = None,
) -> Action:
    valid_idxs = {e.idx for e in elements}

    # Detect loop → escalate to vision if we have a screenshot
    if _is_loop(history) and screenshot_bytes is not None:
        logger.warning("Action loop detected — escalating to vision model")
        system = decider_system(Action)
        user = _build_user_prompt(step, elements, visible_text, history, current_url)
        user += "\n\nWARNING: The same action was repeated with no state change. Look at the screenshot to find an alternative approach."
        result: Action = await router.vision(system, user, screenshot_bytes, Action)  # type: ignore[assignment]
    else:
        system = decider_system(Action)
        user = _build_user_prompt(step, elements, visible_text, history, current_url)
        result: Action = await router.fast(system, user, Action)  # type: ignore[assignment]

    # Validate element_idx
    if (
        result.element_idx is not None
        and result.element_idx not in valid_idxs
        and result.type not in ("done", "cannot_proceed", "goto", "scroll", "wait")
    ):
        logger.warning(
            "Model returned invalid element_idx=%d (valid: %s) — re-asking",
            result.element_idx,
            sorted(valid_idxs),
        )
        # Re-ask once with explicit error
        fix_user = (
            user
            + f"\n\nERROR: element_idx={result.element_idx} does not exist in the list. "
            f"Valid indexes are: {sorted(valid_idxs)}. Choose from the list only."
        )
        result = await router.fast(system, fix_user, Action)  # type: ignore[assignment]

        # If still invalid, bail
        if result.element_idx is not None and result.element_idx not in valid_idxs:
            logger.error("Still invalid after re-ask — returning cannot_proceed")
            return Action(type="cannot_proceed", reasoning="Invalid element index after retry")

    return result
