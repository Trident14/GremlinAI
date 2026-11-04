"""
GremlinAI — SQLite database layer (async via aiosqlite).

Passwords are NEVER stored. Auth state files are deleted after runs.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from gremlinai.models import BugReport, RunMetrics, RunSummary, StepResult, Verdict, Action

logger = logging.getLogger(__name__)

DB_PATH = "gremlinai.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    url TEXT,
    story TEXT,
    status TEXT DEFAULT 'planning',
    auth_mode TEXT,
    auth_username TEXT,
    auth_login_url TEXT,
    started_at TEXT,
    ended_at TEXT,
    metrics_json TEXT
);

CREATE TABLE IF NOT EXISTS step_results (
    id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(id),
    step_id TEXT,
    verdict_status TEXT,
    verdict_evidence TEXT,
    verdict_confidence REAL,
    screenshot_before TEXT,
    screenshot_after TEXT,
    bug_id TEXT,
    duration_ms INTEGER,
    retries INTEGER,
    actions_json TEXT,
    console_errors_json TEXT,
    failed_requests_json TEXT
);

CREATE TABLE IF NOT EXISTS bug_reports (
    id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(id),
    step_id TEXT,
    criterion_id TEXT,
    title TEXT,
    severity TEXT,
    repro_steps_json TEXT,
    expected TEXT,
    actual TEXT,
    screenshot_annotated TEXT,
    page_url TEXT,
    console_errors_json TEXT,
    failed_requests_json TEXT
);
"""


class Database:
    def __init__(self, path: str = DB_PATH) -> None:
        self._path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        logger.info("Database initialised at %s", self._path)

    async def create_run(
        self,
        run_id: str,
        url: str,
        story: str,
        auth_mode: str = "none",
        auth_username: str = "",
        auth_login_url: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO runs (id, url, story, status, auth_mode, auth_username, auth_login_url, started_at) "
                "VALUES (?, ?, ?, 'planning', ?, ?, ?, ?)",
                (run_id, url, story, auth_mode, auth_username, auth_login_url, now),
            )
            await db.commit()

    async def update_run_status(self, run_id: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        final = status not in ("planning", "running")
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE runs SET status=?, ended_at=COALESCE(?, ended_at) WHERE id=?",
                (status, now if final else None, run_id),
            )
            await db.commit()

    async def save_metrics(self, run_id: str, metrics: RunMetrics) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE runs SET metrics_json=? WHERE id=?",
                (metrics.model_dump_json(), run_id),
            )
            await db.commit()

    async def save_step_result(self, run_id: str, result: StepResult) -> None:
        import uuid
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO step_results "
                "(id, run_id, step_id, verdict_status, verdict_evidence, verdict_confidence, "
                "screenshot_before, screenshot_after, duration_ms, retries, "
                "actions_json, console_errors_json, failed_requests_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), run_id, result.step_id,
                    result.verdict.status, result.verdict.evidence, result.verdict.confidence,
                    result.screenshot_before, result.screenshot_after,
                    result.duration_ms, result.retries,
                    json.dumps([a.model_dump() for a in result.actions]),
                    json.dumps(result.console_errors),
                    json.dumps(result.failed_requests),
                ),
            )
            await db.commit()

    async def save_bug_report(self, bug: BugReport) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO bug_reports "
                "(id, run_id, step_id, criterion_id, title, severity, "
                "repro_steps_json, expected, actual, screenshot_annotated, page_url, "
                "console_errors_json, failed_requests_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bug.id, bug.run_id, bug.step_id, bug.criterion_id,
                    bug.title, bug.severity,
                    json.dumps(bug.repro_steps),
                    bug.expected, bug.actual,
                    bug.screenshot_annotated, bug.page_url,
                    json.dumps(bug.console_errors),
                    json.dumps(bug.failed_requests),
                ),
            )
            await db.commit()

    async def get_run(self, run_id: str) -> dict | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM runs WHERE id=?", (run_id,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                return dict(row)

    async def get_step_results(self, run_id: str) -> list[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM step_results WHERE run_id=? ORDER BY rowid", (run_id,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def get_bug_reports(self, run_id: str) -> list[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM bug_reports WHERE run_id=? ORDER BY rowid", (run_id,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def get_bug(self, bug_id: str) -> dict | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM bug_reports WHERE id=?", (bug_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None


db = Database()
