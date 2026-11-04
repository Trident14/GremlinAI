"""
GremlinAI — FastAPI routes (REST + WebSocket).
"""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse

from pydantic import BaseModel
from gremlinai.config import settings
from gremlinai.db import db
from gremlinai.events import EventType, event_bus
from gremlinai.models import AuthConfig, RunCreate, TestPlan

logger = logging.getLogger(__name__)
router = APIRouter()

# Per-run cancel events (run_id → asyncio.Event)
_cancel_events: dict[str, asyncio.Event] = {}


class PlanRequest(BaseModel):
    url: str
    story: str
    auth: AuthConfig | None = None


# ── REST ──────────────────────────────────────────────────────────────────────

@router.get("/api/info")
async def provider_info():
    """Which provider/models are active and what they cost (shown in the UI header)."""
    from gremlinai.llm.router import price_for

    def models(p: str) -> tuple[str, str]:
        if p == "meta":
            return settings.meta_model_fast, settings.meta_model_vision
        if p == "gemini":
            return settings.gemini_model_fast, settings.gemini_model_vision
        return "mock-fast", "mock-vision"

    fp = settings.llm_fast_provider or settings.llm_provider
    vp = settings.llm_vision_provider or settings.llm_provider
    fast, vision = models(fp)[0], models(vp)[1]
    import os
    demo_url = f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/demo/"
    return {
        "demo_url": demo_url,
        "fast_provider": fp, "fast_model": fast, "fast_price": price_for(fast),
        "vision_provider": vp, "vision_model": vision, "vision_price": price_for(vision),
    }


@router.post("/api/plan")
async def generate_plan_preview(body: PlanRequest):
    """
    Generate an editable test plan without running tests yet.
    Allows user to review, edit, add or remove steps before execution.
    """
    from gremlinai.agent.planner import plan
    from gremlinai.llm.router import LLMRouter
    router_llm = LLMRouter(settings)
    try:
        test_plan = await plan(body.story, router_llm, body.auth)
        return test_plan.model_dump()
    except Exception as exc:
        logger.exception("Failed to generate test plan: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to generate test plan: {str(exc)}")
    finally:
        await router_llm.aclose()


@router.post("/api/runs")
async def create_run(body: RunCreate, background_tasks: BackgroundTasks):
    run_id = str(uuid4())
    cancel = asyncio.Event()
    _cancel_events[run_id] = cancel

    auth = body.auth
    await db.create_run(
        run_id=run_id,
        url=body.url,
        story=body.story,
        auth_mode=auth.mode if auth else "none",
        auth_username=auth.username if auth else "",
        auth_login_url=auth.login_url or "" if auth else "",
    )

    from gremlinai.agent.runner import execute_run
    background_tasks.add_task(execute_run, run_id, body, settings, event_bus, cancel, db)

    return {"run_id": run_id, "status": "planning"}


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    row = await db.get_run(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    steps = await db.get_step_results(run_id)
    bugs = await db.get_bug_reports(run_id)
    return {**row, "step_results": steps, "bug_reports": bugs}


@router.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str):
    if run_id in _cancel_events:
        _cancel_events[run_id].set()
        return {"status": "cancelling"}
    raise HTTPException(status_code=404, detail="Run not found or already completed")


@router.get("/api/runs/{run_id}/report")
async def get_report(run_id: str):
    from pathlib import Path
    html_path = Path("runs") / run_id / "signoff_report.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Report not yet generated")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/api/runs/{run_id}/report.pdf")
async def get_report_pdf(run_id: str):
    from pathlib import Path
    pdf_path = Path("runs") / run_id / "signoff_report.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF not yet generated")
    return FileResponse(str(pdf_path), media_type="application/pdf")


@router.post("/api/bugs/{bug_id}/github")
async def export_github(bug_id: str):
    if not settings.github_token:
        raise HTTPException(status_code=400, detail="GITHUB_TOKEN not configured")

    row = await db.get_bug(bug_id)
    if not row:
        raise HTTPException(status_code=404, detail="Bug not found")

    import json
    from gremlinai.models import BugReport
    from gremlinai.reporting.github import create_issue, GitHubNotConfigured

    bug = BugReport(
        id=row["id"],
        run_id=row["run_id"],
        step_id=row["step_id"],
        criterion_id=row.get("criterion_id"),
        title=row["title"],
        severity=row["severity"],
        repro_steps=json.loads(row["repro_steps_json"] or "[]"),
        expected=row["expected"],
        actual=row["actual"],
        screenshot_annotated=row["screenshot_annotated"] or "",
        console_errors=json.loads(row["console_errors_json"] or "[]"),
        failed_requests=json.loads(row["failed_requests_json"] or "[]"),
        page_url=row["page_url"] or "",
    )

    try:
        url = await create_issue(bug, settings.github_token, settings.github_repo)
        return {"issue_url": url}
    except GitHubNotConfigured as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── WebSocket ─────────────────────────────────────────────────────────────────

@router.websocket("/ws/runs/{run_id}")
async def ws_run(websocket: WebSocket, run_id: str):
    q = event_bus.subscribe(run_id)
    await websocket.accept()
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=30)
            except asyncio.TimeoutError:
                # Send a keepalive ping
                try:
                    await websocket.send_text('{"type":"ping"}')
                except Exception:
                    break
                continue

            try:
                await websocket.send_text(event.model_dump_json())
            except Exception:
                break

            if event.type in (EventType.RUN_FINISHED, EventType.RUN_ERROR):
                break
    except WebSocketDisconnect:
        pass
    finally:
        event_bus.unsubscribe(run_id, q)


@router.get("/api/runs/{run_id}/download/{kind}")
async def download_report(run_id: str, kind: str):
    """Download the sign-off report as an attachment (kind = html | pdf)."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    if kind not in ("html", "pdf"):
        raise HTTPException(404, "Unknown format")
    path = Path("runs") / run_id / f"signoff_report.{kind}"
    if not path.exists():
        raise HTTPException(404, f"{kind.upper()} report not available")
    media = "text/html" if kind == "html" else "application/pdf"
    return FileResponse(path, media_type=media, filename=f"GremlinAI_signoff_{run_id[:8]}.{kind}")
