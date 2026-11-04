"""
GremlinAI — planner.

Converts a user story into a TestPlan via the fast LLM.
Injects an implicit Step 0 (Authenticate) when auth.pre_login is True.
"""
from __future__ import annotations
import logging

from gremlinai.llm.router import LLMRouter
from gremlinai.llm.prompts import planner_system
from gremlinai.models import TestPlan, TestStep, Criterion, AuthConfig

logger = logging.getLogger(__name__)


async def plan(
    story: str,
    router: LLMRouter,
    auth: AuthConfig | None = None,
) -> TestPlan:
    """
    Generate a TestPlan from a user story.
    If auth.pre_login is True, inserts Step S0 (Authenticate) at the front.
    """
    system = planner_system(TestPlan)
    result: TestPlan = await router.fast(system, story, TestPlan)  # type: ignore[assignment]

    # Inject auth step if needed
    if auth and auth.mode in ("form",) and auth.pre_login:
        hint = auth.success_hint or "Login form is no longer visible and the URL has changed"
        auth_step = TestStep(
            id="S0",
            criterion_id=None,
            instruction=(
                f"Navigate to {auth.login_url or 'the login page'}, "
                f"enter username {{{{USERNAME}}}} and password {{{{PASSWORD}}}}, "
                f"then submit the form."
            ),
            expected=hint,
        )
        result = TestPlan(
            story=result.story,
            criteria=result.criteria,
            steps=[auth_step] + result.steps,
        )
        logger.info("Injected auth Step S0 (pre_login=True)")

    logger.info(
        "Plan created: %d criteria, %d steps",
        len(result.criteria),
        len(result.steps),
    )
    return result
