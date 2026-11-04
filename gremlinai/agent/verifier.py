"""
GremlinAI — verifier.

Two-pass verification with self-heal before declaring failure.

Pass 1 (cheap): fast model with visible text + URL + element list
Pass 2 (only if unsure/fail): vision model with screenshot
Self-heal: if fail → wait_for_settled → re-ground → re-verify (pass 1)
           Only a second consecutive fail is a confirmed bug.
"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING

from gremlinai.llm.router import LLMRouter
from gremlinai.llm.prompts import verifier_system, verifier_vision_user
from gremlinai.models import ElementRef, TestStep, Verdict
from gremlinai.agent.grounding import format_elements_for_prompt

if TYPE_CHECKING:
    from gremlinai.agent.browser import BrowserSession

logger = logging.getLogger(__name__)


def _build_text_user(
    step: TestStep,
    elements: list[ElementRef],
    visible_text: str,
    url: str,
) -> str:
    element_str = format_elements_for_prompt(elements, visible_text)
    return (
        f"EXPECTED OUTCOME: {step.expected}\n"
        f"CURRENT URL: {url}\n\n"
        f"PAGE STATE:\n{element_str}\n\n"
        f"Is the expected outcome satisfied? Return your verdict as JSON."
    )


async def verify(
    step: TestStep,
    browser: "BrowserSession",
    elements: list[ElementRef],
    visible_text: str,
    router: LLMRouter,
) -> tuple[Verdict, int]:
    """
    Returns (verdict, retries_used).
    retries_used: 0 or 1 (the self-heal attempt).
    """
    url = await browser.current_url()
    system = verifier_system(Verdict)

    # ── Pass 1: text + DOM ────────────────────────────────────────────────────
    user = _build_text_user(step, elements, visible_text, url)
    verdict: Verdict = await router.fast(system, user, Verdict)  # type: ignore[assignment]
    logger.info("Pass 1 verdict: %s (%.2f) — %s", verdict.status, verdict.confidence, verdict.evidence)

    # ── Pass 2: vision (if unsure or fail) ───────────────────────────────────
    if verdict.status in ("unsure", "fail"):
        logger.info("Pass 2: sending screenshot to vision model")
        screenshot = await browser.screenshot_bytes()
        vision_user = verifier_vision_user(step.expected, url)
        vision_verdict: Verdict = await router.vision(system, vision_user, screenshot, Verdict)  # type: ignore[assignment]
        logger.info(
            "Pass 2 verdict: %s (%.2f) — %s",
            vision_verdict.status,
            vision_verdict.confidence,
            vision_verdict.evidence,
        )
        verdict = vision_verdict

    # ── Self-heal: re-ground and re-verify once before declaring fail ─────────
    if verdict.status == "fail":
        logger.info("Self-heal: waiting for settle, re-grounding, re-verifying…")
        await browser.wait_for_settled()

        from gremlinai.agent.grounding import ground
        from playwright.async_api import Page
        page: Page = browser._page  # type: ignore[attr-defined]
        elements2, visible_text2 = await ground(page)
        url2 = await browser.current_url()

        user2 = _build_text_user(step, elements2, visible_text2, url2)
        verdict2: Verdict = await router.fast(system, user2, Verdict)  # type: ignore[assignment]
        logger.info(
            "Self-heal verdict: %s (%.2f) — %s",
            verdict2.status,
            verdict2.confidence,
            verdict2.evidence,
        )

        if verdict2.status != "fail":
            return verdict2, 1  # healed!

        # Still fail after self-heal — confirmed
        return verdict2, 1

    return verdict, 0
