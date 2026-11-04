"""
GremlinAI CLI entry point for testing single steps and full stories.
"""
from __future__ import annotations
import argparse
import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from gremlinai.config import settings
from gremlinai.events import event_bus
from gremlinai.models import Action, AuthConfig, ElementRef, RunCreate, TestStep
from gremlinai.agent.browser import BrowserSession
from gremlinai.agent.grounding import ground, format_elements_for_prompt
from gremlinai.agent.decider import decide
from gremlinai.llm.router import LLMRouter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("gremlinai.cli")

async def run_single_step(url: str, step_instruction: str, expected: str = ""):
    run_id = f"cli-{uuid4().hex[:6]}"
    run_dir = Path("runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    router = LLMRouter(settings)
    browser = BrowserSession(run_dir=run_dir)
    await browser.start()

    try:
        logger.info(f"Navigating to {url}...")
        await browser.goto(url)

        step = TestStep(
            id="S1",
            criterion_id="AC1",
            instruction=step_instruction,
            expected=expected or "Instruction is fulfilled"
        )

        history: list[Action] = []
        for attempt in range(settings.max_actions_per_step):
            page = browser._page
            elements, visible_text = await ground(page)
            curr_url = await browser.current_url()

            print(f"\n--- Attempt {attempt + 1} Grounding ---")
            print(f"URL: {curr_url}")
            print(f"Elements found: {len(elements)}")
            print(f"Visible text: {visible_text}")

            action = await decide(step, elements, visible_text, history, curr_url, router)
            print(f"Decision: {action.type} (idx: {action.element_idx}, text: {action.text}) - {action.reasoning}")

            if action.type == "done":
                print("Step marked as DONE by decider.")
                break
            if action.type == "cannot_proceed":
                print("Step CANNOT PROCEED.")
                break

            # Execute action
            from gremlinai.agent.runner import _execute_action
            await _execute_action(browser, action, elements)
            history.append(action)
            await asyncio.sleep(0.5)

        print("\nFinished CLI step run.")
    finally:
        await browser.close()
        await router.aclose()

def main():
    parser = argparse.ArgumentParser(description="GremlinAI CLI")
    parser.add_argument("--url", required=True, help="Target URL")
    parser.add_argument("--step", required=True, help="Step instruction in plain English")
    parser.add_argument("--expected", default="", help="Expected outcome")
    args = parser.parse_args()

    asyncio.run(run_single_step(args.url, args.step, args.expected))

if __name__ == "__main__":
    main()
