"""
GremlinAI — all prompt templates.

Provider-agnostic: no Gemini/Meta-specific formatting.
All prompts demand JSON-only output; the client strips fences and validates.
"""
import json
from typing import Type
from pydantic import BaseModel


def _schema_str(schema: Type[BaseModel]) -> str:
    return json.dumps(schema.model_json_schema(), indent=2)


def planner_system(schema: Type[BaseModel]) -> str:
    return f"""You are a senior QA engineer. Convert the user story into a structured test plan.

Return ONLY valid JSON — no markdown, no code fences, no explanation.
The JSON must exactly match this schema:
{_schema_str(schema)}

Rules:
- Generate 3 to 10 steps
- Each step must have a single user-level instruction and an OBSERVABLE expected outcome (something visible on the page or in the URL)
- Map every step to an acceptance criterion id from the criteria list
- If the story has no explicit acceptance criteria, derive them first, then map steps to them
- Steps must be sequential and build on each other"""


def decider_system(schema: Type[BaseModel]) -> str:
    return f"""You control a web browser to execute ONE test step.

Return ONLY valid JSON — no markdown, no code fences, no explanation.
The JSON must exactly match this schema:
{_schema_str(schema)}

Rules:
- Choose exactly ONE next action
- Only use element_idx values that appear in the provided element list — NEVER invent an index
- If the step instruction is already complete based on the page state, return type "done"
- If the step cannot be completed with the available elements, return type "cannot_proceed"
- For type actions: use {{{{USERNAME}}}} and {{{{PASSWORD}}}} placeholders for credentials — never real values
- Elements marked "disabled" cannot be clicked or typed into. If the button you need is disabled, FIRST fill the required fields (for example type the password) and click it only once it is no longer disabled
- If RECENT ACTIONS shows "[FAILED" or the same action had no effect, choose a DIFFERENT action instead of repeating it
- Do NOT judge whether the app is behaving correctly — only perform the step
- Include a short "reasoning" string (shown in the UI)"""


def verifier_system(schema: Type[BaseModel]) -> str:
    return f"""You are verifying an acceptance test step outcome.

Return ONLY valid JSON — no markdown, no code fences, no explanation.
The JSON must exactly match this schema:
{_schema_str(schema)}

Rules:
- Decide: pass, fail, blocked, or unsure
- "pass": the expected outcome is clearly and unambiguously present
- "fail": the app behaved incorrectly (expected outcome absent or wrong)
- "blocked": GremlinAI could not perform the step (element not found, page error, etc.) — this is NOT a bug
- "unsure": low confidence, cannot determine from available evidence
- Be STRICT: if the expected outcome is not clearly present, it is NOT a pass
- Cite exactly one concrete sentence of evidence from the observed page state
- "confidence" is a float 0.0–1.0"""


def verifier_vision_user(expected: str, url: str) -> str:
    return (
        f"Expected outcome: {expected}\n"
        f"Current URL: {url}\n\n"
        f"Look at the screenshot and determine if the expected outcome is satisfied. "
        f"Return your verdict as JSON."
    )


def bug_title_system() -> str:
    return (
        'You write concise bug titles. Return ONLY JSON: {"title": "...", "severity": "critical|major|minor"}\n'
        "Title max 80 characters. Severity: critical=blocks a core criterion, major=wrong data/behavior, minor=cosmetic."
    )


def bug_title_user(expected: str, actual: str) -> str:
    return f"Expected: {expected}\nActual observed: {actual}"
