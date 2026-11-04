"""
GremlinAI — all pydantic data models.
"""
from __future__ import annotations
from typing import Literal, Any
from pydantic import BaseModel, Field


# ── Test plan ─────────────────────────────────────────────────────────────────

class Criterion(BaseModel):
    id: str           # "AC1"
    text: str         # original acceptance criterion


class TestStep(BaseModel):
    id: str                       # "S1"
    criterion_id: str | None = None
    instruction: str              # "Enter a valid email and submit"
    expected: str                 # "A success banner is visible"


class TestPlan(BaseModel):
    story: str
    criteria: list[Criterion]
    steps: list[TestStep]


# ── Browser grounding ─────────────────────────────────────────────────────────

class ElementRef(BaseModel):
    idx: int
    role: str                      # button, textbox, link, checkbox, combobox…
    name: str                      # accessible name
    placeholder: str | None = None
    value: str | None = None
    bbox: tuple[float, float, float, float]  # x, y, w, h
    selector: str                  # stable CSS / Playwright selector
    visible: bool
    enabled: bool


# ── Agent actions ─────────────────────────────────────────────────────────────

class Action(BaseModel):
    type: Literal[
        "click", "type", "select", "press",
        "goto", "scroll", "wait", "done", "cannot_proceed"
    ]
    element_idx: int | None = None
    text: str | None = None        # for type / select / press
    reasoning: str = ""            # shown in the UI


# ── Verification ──────────────────────────────────────────────────────────────

class Verdict(BaseModel):
    status: Literal["pass", "fail", "blocked", "unsure"]
    evidence: str                  # one sentence citing what was observed
    confidence: float = 1.0


# ── Step result ───────────────────────────────────────────────────────────────

class StepResult(BaseModel):
    step_id: str
    actions: list[Action] = Field(default_factory=list)
    verdict: Verdict
    screenshot_before: str = ""    # file path
    screenshot_after: str = ""
    console_errors: list[str] = Field(default_factory=list)
    failed_requests: list[dict] = Field(default_factory=list)
    duration_ms: int = 0
    retries: int = 0


# ── Bug report ────────────────────────────────────────────────────────────────

class BugReport(BaseModel):
    id: str
    run_id: str
    step_id: str
    criterion_id: str | None = None
    title: str
    severity: Literal["critical", "major", "minor"]
    repro_steps: list[str] = Field(default_factory=list)
    expected: str
    actual: str
    screenshot_annotated: str = ""  # file path
    console_errors: list[str] = Field(default_factory=list)
    failed_requests: list[dict] = Field(default_factory=list)
    page_url: str = ""


# ── Run metrics ───────────────────────────────────────────────────────────────

class RunMetrics(BaseModel):
    fast_calls: int = 0
    vision_calls: int = 0
    avg_latency_ms: float = 0.0
    total_cost_usd: float | None = None   # None = "unknown"
    provider: str = ""
    fast_model: str = ""
    vision_model: str = ""


# ── Auth config ───────────────────────────────────────────────────────────────

class AuthConfig(BaseModel):
    mode: Literal["none", "form", "storage_state"] = "none"
    login_url: str | None = None
    username: str = ""
    extra_fields: dict[str, str] = Field(default_factory=dict)
    success_hint: str | None = None
    # Default to testing the login journey. Set True only when the user explicitly
    # wants to begin the UAT run from an already-authenticated state.
    pre_login: bool = False
    # password is NOT stored here — held in-memory on the Run object


# ── API schemas ───────────────────────────────────────────────────────────────

class RunCreate(BaseModel):
    url: str
    story: str
    auth: AuthConfig | None = None
    # password passed separately so it's never in any persisted model
    password: str = ""
    # Optional pre-reviewed / user-edited plan
    plan: TestPlan | None = None


class RunSummary(BaseModel):
    id: str
    url: str
    story: str
    status: str
    started_at: str | None = None
    ended_at: str | None = None
    metrics: RunMetrics | None = None
    step_results: list[StepResult] = Field(default_factory=list)
    bug_reports: list[BugReport] = Field(default_factory=list)
