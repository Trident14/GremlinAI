"""
GremlinAI — bug report builder.

Annotates screenshots with Pillow, calls the fast LLM for title + severity,
and assembles the full BugReport model.
"""
from __future__ import annotations
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

from gremlinai.models import BugReport, ElementRef, StepResult, TestStep

if TYPE_CHECKING:
    from gremlinai.llm.router import LLMRouter

logger = logging.getLogger(__name__)


async def build_bug_report(
    run_id: str,
    step: TestStep,
    result: StepResult,
    elements: list[ElementRef],
    router: "LLMRouter",
    run_dir: Path,
) -> BugReport:
    bug_id = f"BUG-{uuid.uuid4().hex[:8].upper()}"

    # ── Annotate screenshot ───────────────────────────────────────────────────
    annotated_path = await _annotate_screenshot(
        result.screenshot_after, elements, result.actions, run_dir, bug_id
    )

    # ── LLM: title + severity ─────────────────────────────────────────────────
    from gremlinai.llm.prompts import bug_title_system, bug_title_user
    from pydantic import BaseModel

    class _BugMeta(BaseModel):
        title: str
        severity: str

    try:
        system = bug_title_system()
        user = bug_title_user(step.expected, result.verdict.evidence)
        meta: _BugMeta = await router.fast(system, user, _BugMeta)  # type: ignore[assignment]
        title = meta.title[:80]
        severity = meta.severity if meta.severity in ("critical", "major", "minor") else "major"
    except Exception as exc:
        logger.warning("Bug title LLM call failed: %s", exc)
        title = f"Step {step.id} failed: {step.expected[:60]}"
        severity = "major"

    # ── Repro steps from action trail ─────────────────────────────────────────
    repro: list[str] = []
    for action in result.actions:
        el_name = ""
        for el in elements:
            if el.idx == action.element_idx:
                el_name = f' "{el.name}"'
                break
        repro.append(f"{action.type.capitalize()}{el_name}: {action.reasoning}")

    return BugReport(
        id=bug_id,
        run_id=run_id,
        step_id=step.id,
        criterion_id=step.criterion_id,
        title=title,
        severity=severity,  # type: ignore[arg-type]
        repro_steps=repro,
        expected=step.expected,
        actual=result.verdict.evidence,
        screenshot_annotated=str(annotated_path),
        console_errors=result.console_errors,
        failed_requests=result.failed_requests,
        page_url="",
    )


async def _annotate_screenshot(
    screenshot_path: str,
    elements: list[ElementRef],
    actions: list,
    run_dir: Path,
    bug_id: str,
) -> Path:
    """Draw a red rectangle on the last interacted element's bbox."""
    try:
        img = Image.open(screenshot_path)
        draw = ImageDraw.Draw(img)

        # Find the last action with an element
        target_bbox = None
        for action in reversed(actions):
            if action.element_idx is not None:
                for el in elements:
                    if el.idx == action.element_idx:
                        target_bbox = el.bbox
                        break
            if target_bbox:
                break

        if target_bbox:
            x, y, w, h = target_bbox
            draw.rectangle([x, y, x + w, y + h], outline="red", width=3)

        out_path = run_dir / f"{bug_id}_annotated.jpg"
        img.save(str(out_path), "JPEG", quality=85)
        return out_path
    except Exception as exc:
        logger.warning("Screenshot annotation failed: %s", exc)
        return Path(screenshot_path)
