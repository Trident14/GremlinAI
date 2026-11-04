"""
GremlinAI — sign-off report builder.

Generates an HTML report (Jinja2) with a traceability matrix, bug details,
and run metrics. Also exports a PDF via Playwright's page.pdf().
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, select_autoescape

from gremlinai.models import BugReport, RunCreate, RunMetrics, StepResult, TestPlan

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


def _data_uri(path: Path, max_w: int = 1000) -> str:
    """Embed an image in the HTML (base64) so the report works wherever it is opened:
    from /api/runs/<id>/report, downloaded, emailed, or converted to PDF."""
    import base64
    import io

    try:
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGB")
            if im.width > max_w:
                im = im.resize((max_w, round(im.height * max_w / im.width)))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=78, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:  # unreadable / missing image -> report still renders
        logger.warning("Could not embed image %s: %s", path, exc)
        return ""


def _overall_verdict(results: list[StepResult]) -> str:
    statuses = {r.verdict.status for r in results}
    if "fail" in statuses:
        return "REJECTED"
    if "blocked" in statuses or "unsure" in statuses:
        return "NEEDS REVIEW"
    return "APPROVED"


async def build_signoff_report(
    run_id: str,
    body: RunCreate,
    plan: TestPlan,
    results: list[StepResult],
    bugs: list[BugReport],
    metrics: RunMetrics,
    run_dir: Path,
) -> Path:
    verdict_map = {r.step_id: r for r in results}
    bug_map = {b.step_id: b for b in bugs}
    overall = _overall_verdict(results)

    # Build traceability matrix rows
    matrix = []
    for criterion in plan.criteria:
        steps_for = [s for s in plan.steps if s.criterion_id == criterion.id]
        step_verdicts = [verdict_map.get(s.id) for s in steps_for if verdict_map.get(s.id)]
        statuses = [r.verdict.status for r in step_verdicts if r]
        if "fail" in statuses:
            cr_status = "fail"
        elif "blocked" in statuses or "unsure" in statuses:
            cr_status = "needs_review"
        elif statuses:
            cr_status = "pass"
        else:
            cr_status = "pending"

        matrix.append({
            "criterion": criterion,
            "steps": steps_for,
            "results": step_verdicts,
            "status": cr_status,
        })

    # Build rich step execution evidence records
    step_records = []
    for step in plan.steps:
        res = verdict_map.get(step.id)
        before_file = Path(res.screenshot_before).name if (res and res.screenshot_before) else f"step_{step.id}_before.jpg"
        after_file = Path(res.screenshot_after).name if (res and res.screenshot_after) else f"step_{step.id}_after.jpg"
        step_records.append({
            "step": step,
            "result": res,
            "status": res.verdict.status if res else "pending",
            "evidence": res.verdict.evidence if res else "Not executed",
            "actions": res.actions if res else [],
            "duration_ms": res.duration_ms if res else 0,
            "before_img": _data_uri(run_dir / before_file) if (run_dir / before_file).exists() else "",
            "after_img": _data_uri(run_dir / after_file) if (run_dir / after_file).exists() else "",
            "bug": bug_map.get(step.id),
        })

    # Prepare relative bug screenshot paths
    clean_bugs = []
    for b in bugs:
        b_dict = b.model_dump()
        if b.screenshot_annotated:
            b_dict["screenshot_file"] = _data_uri(run_dir / Path(b.screenshot_annotated).name)
        clean_bugs.append(b_dict)

    # Compute KPI statistics
    total_steps = len(plan.steps)
    passed_steps = sum(1 for r in results if r.verdict.status == "pass")
    failed_steps = sum(1 for r in results if r.verdict.status == "fail")
    pass_rate = round((passed_steps / total_steps) * 100) if total_steps > 0 else 0
    total_duration_sec = round(sum(r.duration_ms for r in results) / 1000, 1)

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )

    template = env.get_template("signoff.html")
    html = template.render(
        run_id=run_id,
        story=body.story,
        url=body.url,
        date=datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC"),
        overall=overall,
        matrix=matrix,
        plan=plan,
        results=list(results),
        step_records=step_records,
        bugs=clean_bugs,
        metrics=metrics,
        pass_rate=pass_rate,
        passed_steps=passed_steps,
        failed_steps=failed_steps,
        total_steps=total_steps,
        total_duration_sec=total_duration_sec,
    )

    html_path = run_dir / "signoff_report.html"
    html_path.write_text(html, encoding="utf-8")
    logger.info("Sign-off report written: %s", html_path)

    # PDF export
    pdf_path = run_dir / "signoff_report.pdf"
    try:
        await _export_pdf(html_path, pdf_path)
    except Exception as exc:
        logger.warning("PDF export failed (non-fatal): %s", exc)

    return html_path


async def _export_pdf(html_path: Path, pdf_path: Path) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        page = await browser.new_page()
        await page.goto(f"file://{html_path.resolve()}")
        await page.pdf(path=str(pdf_path), format="A4", print_background=True)
        await browser.close()
    logger.info("PDF report written: %s", pdf_path)
