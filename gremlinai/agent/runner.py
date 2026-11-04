"""
GremlinAI — runner (orchestration loop).

Executes a TestPlan step by step, enforcing all limits and emitting events.
Never crashes the server — wraps everything and emits run_error.
"""
from __future__ import annotations
import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from gremlinai.config import Settings
from gremlinai.events import EventBus, EventType
from gremlinai.models import (
    Action, AuthConfig, BugReport, RunCreate, RunMetrics,
    StepResult, TestPlan, TestStep, Verdict,
)

if TYPE_CHECKING:
    from gremlinai.agent.browser import BrowserSession
    from gremlinai.llm.router import LLMRouter

logger = logging.getLogger(__name__)


async def execute_run(
    run_id: str,
    body: RunCreate,
    settings: Settings,
    bus: EventBus,
    cancel_event: asyncio.Event,
    db,  # gremlinai.db.Database
) -> None:
    """Top-level run coroutine. Wraps everything; emits run_error on any failure."""
    try:
        await _run(run_id, body, settings, bus, cancel_event, db)
    except asyncio.CancelledError:
        await bus.publish_simple(run_id, EventType.RUN_FINISHED, {"status": "cancelled"})
        await db.update_run_status(run_id, "cancelled")
    except Exception as exc:
        logger.exception("Unhandled error in run %s", run_id)
        await bus.publish_simple(run_id, EventType.RUN_ERROR, {"error": str(exc)})
        await db.update_run_status(run_id, "failed")


async def _run(
    run_id: str,
    body: RunCreate,
    settings: Settings,
    bus: EventBus,
    cancel_event: asyncio.Event,
    db,
) -> None:
    from gremlinai.agent.browser import BrowserSession
    from gremlinai.agent.grounding import ground
    from gremlinai.agent.planner import plan
    from gremlinai.agent.decider import decide
    from gremlinai.agent.verifier import verify
    from gremlinai.llm.router import LLMRouter
    from gremlinai.reporting.bugs import build_bug_report

    run_dir = Path("runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    router = LLMRouter(settings)
    auth: AuthConfig | None = body.auth
    password: str = body.password  # in-memory only

    # ── Browser ───────────────────────────────────────────────────────────────
    browser = BrowserSession(
        run_dir=run_dir,
        username=auth.username if auth else "",
        password=password,
        extra_fields=auth.extra_fields if auth else {},
    )
    await browser.start()
    browser.start_streaming(run_id, bus)

    try:
        await db.update_run_status(run_id, "planning")

        # Navigate immediately so the user can see their target site live in the preview while planning
        try:
            await browser.goto(body.url)
            await browser.emit_frame(run_id, bus)
        except Exception as exc:
            logger.warning("Initial goto failed: %s", exc)

        # ── Plan ──────────────────────────────────────────────────────────────
        if body.plan and body.plan.steps:
            test_plan = body.plan
            logger.info("Using user-confirmed test plan with %d steps", len(test_plan.steps))
        else:
            try:
                test_plan = await plan(body.story, router, auth)
            except Exception as exc:
                logger.error("Planner failed: %s", exc)
                await bus.publish_simple(run_id, EventType.RUN_ERROR, {
                    "error": "planning_failed",
                    "message": f"AI planner error: {exc}. LLM provider may not be responding.",
                })
                await db.update_run_status(run_id, "planning_failed")
                return

        await bus.publish_simple(run_id, EventType.PLAN_CREATED, {
            "story": test_plan.story,
            "criteria": [c.model_dump() for c in test_plan.criteria],
            "steps": [s.model_dump() for s in test_plan.steps],
        })
        await db.update_run_status(run_id, "running")

        # Ensure page is at target URL before starting steps
        if await browser.current_url() != body.url:
            await browser.goto(body.url)
            await browser.emit_frame(run_id, bus)

        # ── Step loop ─────────────────────────────────────────────────────────
        total_actions = 0
        all_results: list[StepResult] = []
        all_bugs: list[BugReport] = []
        auth_failed = False

        async def _run_steps():
            nonlocal total_actions, auth_failed
            consecutive_forced = 0

            for step in test_plan.steps:
                if cancel_event.is_set():
                    break
                if total_actions >= settings.max_total_actions:
                    logger.warning("Total action cap reached")
                    break

                step_start = time.monotonic()
                await bus.publish_simple(run_id, EventType.STEP_STARTED, step.model_dump())

                shot_before = f"step_{step.id}_before.jpg"
                await browser.screenshot(shot_before)

                history: list[Action] = []
                STEP_HARD_TIMEOUT = settings.step_timeout_s + 60  # absolute cap for one step's action loop

                async def _act_loop() -> tuple[str | None, bool]:
                    """Run the agent loop for this step.
                    Returns (failure_reason, systemic). failure_reason is None when the loop
                    ended normally. systemic=True means later steps would fail too (LLM down, budget)."""
                    nonlocal total_actions
                    consecutive_same_action = 0
                    last_action_sig = None

                    for attempt in range(settings.max_actions_per_step):
                        if cancel_event.is_set():
                            break
                        if total_actions >= settings.max_total_actions:
                            break

                        # Soft step timeout: agent is taking too long
                        if (time.monotonic() - step_start) > settings.step_timeout_s:
                            logger.error("Step %s stalled (>%ss). AI appears stuck.", step.id, settings.step_timeout_s)
                            return (f"AI got stuck: step {step.id} ran for over {settings.step_timeout_s} seconds without completing.", False)

                        # Budget check
                        if router.budget_exceeded():
                            return ("Run stopped: AI cost budget exceeded.", True)

                        page = browser._page  # type: ignore[attr-defined]
                        elements, visible_text = await ground(page)

                        screenshot_bytes = None
                        if (
                            len(history) >= 2
                            and history[-1].type == history[-2].type
                            and history[-1].element_idx == history[-2].element_idx
                        ):
                            # Agent looks stuck -> let the vision model see the page
                            try:
                                screenshot_bytes = await browser.screenshot_bytes()
                            except Exception:
                                screenshot_bytes = None
                        try:
                            action = await decide(
                                step, elements, visible_text, history,
                                await browser.current_url(), router,
                                screenshot_bytes=screenshot_bytes,
                            )
                        except Exception as exc:
                            logger.error("Decider LLM error in step %s: %s", step.id, exc)
                            return (f"AI could not decide the next action: LLM call failed ({str(exc)[:150]}).", True)

                        if action.type == "done":
                            break
                        if action.type == "cannot_proceed":
                            logger.info("Step %s: cannot_proceed", step.id)
                            break

                        # Stagnation check: same exact action 3 times in a row
                        action_sig = (action.type, action.element_idx, action.text)
                        if action_sig == last_action_sig:
                            consecutive_same_action += 1
                            if consecutive_same_action >= 3:
                                logger.error("Step %s: AI stuck repeating %s", step.id, action_sig)
                                return (
                                    f"AI got stuck: it repeated '{action.type}' on element [{action.element_idx}] "
                                    f"3 times without making progress.", False)
                        else:
                            consecutive_same_action = 1
                            last_action_sig = action_sig

                        # Execute the action
                        try:
                            await _execute_action(browser, action, elements)
                        except Exception as exc:
                            logger.warning("Action failed: %s", exc)
                            action.reasoning = f"{action.reasoning} [FAILED: {str(exc)[:120]}]"

                        await browser.emit_frame(run_id, bus)

                        history.append(action)
                        total_actions += 1
                        await bus.publish_simple(run_id, EventType.ACTION_EXECUTED, {
                            "step_id": step.id,
                            "action": action.model_dump(),
                            "bbox": _bbox_for_action(action, elements),
                        })
                    return (None, False)

                forced_fail: str | None = None
                systemic = False
                try:
                    forced_fail, systemic = await asyncio.wait_for(_act_loop(), timeout=STEP_HARD_TIMEOUT)
                except asyncio.TimeoutError:
                    forced_fail = f"AI got stuck: step {step.id} timed out after {STEP_HARD_TIMEOUT} seconds."
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # never let one step kill the whole run
                    logger.exception("Step %s crashed", step.id)
                    forced_fail = f"Step crashed unexpectedly: {str(exc)[:150]}"

                # Verify (or mark failed if the agent got stuck)
                elements_after, visible_text_after = [], ""
                try:
                    page = browser._page  # type: ignore[attr-defined]
                    elements_after, visible_text_after = await ground(page)
                except Exception:
                    pass

                retries = 0
                if forced_fail:
                    # LLM/budget problems are tooling issues, not app defects -> 'blocked' (no bug report)
                    verdict = Verdict(
                        status="blocked" if systemic else "fail",
                        evidence=("Not an app defect - testing tool problem. " if systemic else "") + forced_fail,
                        confidence=1.0,
                    )
                else:
                    try:
                        verdict, retries = await verify(
                            step, browser, elements_after, visible_text_after, router
                        )
                    except Exception as exc:
                        logger.error("Verifier LLM error in step %s: %s", step.id, exc)
                        systemic = True
                        verdict = Verdict(
                            status="blocked", confidence=1.0,
                            evidence=f"Not an app defect - AI verifier could not judge this step: LLM call failed ({str(exc)[:150]}).",
                        )

                shot_after = f"step_{step.id}_after.jpg"
                try:
                    await browser.screenshot(shot_after)
                except Exception as exc:
                    logger.warning("After-screenshot failed: %s", exc)
                    shot_after = shot_before

                rel_shot_before = f"runs/{run_id}/{shot_before}"
                rel_shot_after = f"runs/{run_id}/{shot_after}"

                duration_ms = int((time.monotonic() - step_start) * 1000)
                result = StepResult(
                    step_id=step.id,
                    actions=history,
                    verdict=verdict,
                    screenshot_before=str(run_dir / shot_before),
                    screenshot_after=str(run_dir / shot_after),
                    console_errors=list(browser.console_errors),
                    failed_requests=list(browser.failed_requests),
                    duration_ms=duration_ms,
                    retries=retries,
                )
                all_results.append(result)
                await db.save_step_result(run_id, result)

                # Auth failure (Step S0): record it, skip remaining steps, still produce a report
                if step.id == "S0" and verdict.status == "fail":
                    logger.error("Auth failed — remaining steps will be skipped")
                    auth_failed = True
                    verdict.evidence = "Login failed - check credentials. " + verdict.evidence
                    await db.update_run_status(run_id, "auth_failed")

                await bus.publish_simple(run_id, EventType.VERDICT, {
                    "step_id": step.id,
                    "verdict": verdict.model_dump(),
                    "screenshot_before": rel_shot_before,
                    "screenshot_after": rel_shot_after,
                })

                # Bug report on confirmed failure
                if verdict.status == "fail":
                    bug = await build_bug_report(
                        run_id=run_id,
                        step=step,
                        result=result,
                        elements=elements_after,
                        router=router,
                        run_dir=run_dir,
                    )
                    try:
                        bug.page_url = await browser.current_url()
                    except Exception:
                        pass
                    all_bugs.append(bug)
                    await db.save_bug_report(bug)
                    await bus.publish_simple(run_id, EventType.BUG_LOGGED, bug.model_dump())

                await bus.publish_simple(run_id, EventType.STEP_FINISHED, {
                    "step_id": step.id,
                    "verdict": verdict.status,
                    "duration_ms": duration_ms,
                    "screenshot_before": rel_shot_before,
                    "screenshot_after": rel_shot_after,
                    "evidence": verdict.evidence,
                })

                # Reset per-step error accumulators
                browser.console_errors.clear()
                browser.failed_requests.clear()

                if forced_fail:
                    consecutive_forced += 1
                else:
                    consecutive_forced = 0
                if auth_failed or systemic or consecutive_forced >= 2:
                    logger.warning("Stopping remaining steps (auth=%s systemic=%s stuck_in_row=%d)",
                                   auth_failed, systemic, consecutive_forced)
                    break

        # Run with global timeout
        try:
            await asyncio.wait_for(
                _run_steps(),
                timeout=settings.max_run_minutes * 60,
            )
        except asyncio.TimeoutError:
            logger.warning("Run %s timed out after %d minutes", run_id, settings.max_run_minutes)
            await db.update_run_status(run_id, "timeout")

        # ── Final report ──────────────────────────────────────────────────────
        final_metrics = router.aggregate()
        await db.save_metrics(run_id, final_metrics)

        from gremlinai.reporting.signoff import build_signoff_report
        report_path = await build_signoff_report(
            run_id=run_id,
            body=body,
            plan=test_plan,
            results=all_results,
            bugs=all_bugs,
            metrics=final_metrics,
            run_dir=run_dir,
        )

        overall = _overall_verdict(all_results)
        _row = await db.get_run(run_id)
        if _row and _row.get("status") in ("running", "planning", None):
            await db.update_run_status(run_id, "completed")

        await bus.publish_simple(run_id, EventType.METRICS, final_metrics.model_dump())
        await bus.publish_simple(run_id, EventType.RUN_FINISHED, {
            "run_id": run_id,
            "status": overall,
            "report_path": str(report_path),
            "metrics": final_metrics.model_dump(),
        })

    finally:
        # Clean up auth state unless KEEP_AUTH_STATE=true
        auth_state = run_dir / "auth_state.json"
        if auth_state.exists() and not settings.keep_auth_state:
            auth_state.unlink()

        browser.stop_streaming()
        await browser.close()
        await router.aclose()


async def _execute_action(
    browser: "BrowserSession",
    action: Action,
    elements: list,
) -> None:
    """Dispatch an Action to the BrowserSession."""
    sel = None
    if action.element_idx is not None:
        for el in elements:
            if el.idx == action.element_idx:
                sel = el.selector
                break

    target = next((e for e in elements if e.idx == action.element_idx), None) \
        if action.element_idx is not None else None
    if target is not None and action.type in ("click", "type", "select", "press") and not target.enabled:
        raise RuntimeError(
            "target element is disabled - fill the required fields first (e.g. the password) so it becomes enabled"
        )

    if action.type == "click":
        if sel:
            await browser.click(sel)
    elif action.type == "type":
        if sel and action.text:
            await browser.type_text(sel, action.text)
    elif action.type == "select":
        if sel and action.text:
            await browser.select_option(sel, action.text)
    elif action.type == "press":
        if sel and action.text:
            await browser.press_key(sel, action.text)
    elif action.type == "goto":
        if action.text:
            await browser.goto(action.text)
    elif action.type == "scroll":
        direction = "down"
        if action.text and "up" in action.text.lower():
            direction = "up"
        await browser.scroll(direction)
    elif action.type == "wait":
        await browser.wait(1000)


def _bbox_for_action(action: Action, elements: list) -> list | None:
    if action.element_idx is None:
        return None
    for el in elements:
        if el.idx == action.element_idx:
            return list(el.bbox)
    return None


def _overall_verdict(results: list[StepResult]) -> str:
    statuses = {r.verdict.status for r in results}
    if "fail" in statuses:
        return "REJECTED"
    if "blocked" in statuses or "unsure" in statuses:
        return "NEEDS REVIEW"
    return "APPROVED"
